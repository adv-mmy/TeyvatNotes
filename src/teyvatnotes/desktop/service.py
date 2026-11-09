"""桌面组合根：仅本地会话和共享攻略 HTTPS 客户端。"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationService,
    ConversationSubmission,
    ConversationSummary,
    GuideConversation,
    QuestionPatch,
)
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.application.question_understanding import QuestionUnderstandingService
from teyvatnotes.desktop.client import RemoteGuideAnswerer
from teyvatnotes.knowledge.terminology import load_catalog
from teyvatnotes.repositories.conversation_sqlite import SqliteConversationRepository

PAGE_SIZE = 24


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[ConversationSummary, ...]
    offset: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class SubmitResult:
    conversation: GuideConversation
    saved: bool = True
    notice: str = ""


class Workbench:
    """启动只初始化本机 SQLite，脱离服务器仍可翻阅已保存的会话。"""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._conversation_service: ConversationService | None = None
        self._transport = transport

    async def connect(self, directory: Path) -> Page:
        repository = SqliteConversationRepository(directory / "client" / "conversations.sqlite3")
        owner = await repository.initialize()
        self._conversation_service = ConversationService(
            repository,
            owner,
            RemoteGuideAnswerer(directory, self._transport),
            understanding_factory=lambda: QuestionUnderstandingService(load_catalog()),
            timeout_seconds=115,
        )
        return await self.page()

    async def close(self) -> None:
        self._conversation_service = None

    def _service(self) -> ConversationService:
        if self._conversation_service is None:
            raise ConversationError("conversation_storage_unavailable")
        return self._conversation_service

    async def page(self, offset: int = 0) -> Page:
        items = await self._service().list_recent(offset=offset, limit=PAGE_SIZE + 1)
        return Page(items[:PAGE_SIZE], offset, len(items) > PAGE_SIZE)

    async def get(self, identifier: UUID) -> GuideConversation:
        return await self._service().get(identifier)

    async def interrupt(self, conversation: GuideConversation) -> SubmitResult:
        result = await self._service().interrupt(
            conversation.id, expected_revision=conversation.revision
        )
        return SubmitResult(result, notice="已标记本轮中断，可以在核对历史后继续。")

    async def submit(
        self, conversation: GuideConversation | None, question: str | QuestionPatch
    ) -> SubmitResult:
        service = self._service()
        if conversation is None:
            if not isinstance(question, str):
                raise ConversationError("invalid_conversation_input")
            conversation = await service.create(QuestionRequest(question))
            assert conversation is not None
        submission = ConversationSubmission()
        try:
            result = await service.ask(
                conversation.id,
                question,
                expected_revision=conversation.revision,
                submission=submission,
            )
        except ConversationError as error:
            if error.code not in {
                "conversation_conflict",
                "conversation_busy",
                "conversation_closed",
            }:
                raise
            current = await service.get(conversation.id)
            return SubmitResult(current, False, "会话已有更新。已刷新历史，请核对后再提交。")
        if result is None:
            clarification = submission.understanding
            hints = "\n".join(clarification.questions) if clarification else ""
            return SubmitResult(conversation, False, hints or "请补充明确的角色、任务或版本。")
        return SubmitResult(result)
