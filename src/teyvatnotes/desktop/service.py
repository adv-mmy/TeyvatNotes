"""桌面会话组合根，实际问答仍走现有会话、检索和模型服务。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationService,
    ConversationSubmission,
    ConversationSummary,
    GuideConversation,
    QuestionPatch,
)
from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.application.question_understanding import QuestionUnderstandingService
from teyvatnotes.application.tool_model_ports import ToolModelClient
from teyvatnotes.desktop.config import DesktopError, load_config
from teyvatnotes.desktop.library import load_library
from teyvatnotes.domain import JsonValue
from teyvatnotes.domain.guides import GuideKind
from teyvatnotes.knowledge.conversation_cli import GuideConversationAnswerer
from teyvatnotes.knowledge.terminology import load_catalog
from teyvatnotes.models.responses import ResponsesSettings
from teyvatnotes.models.responses_tools import ResponsesToolClient
from teyvatnotes.repositories.conversation_memory import InMemoryConversationRepository
from teyvatnotes.repositories.conversation_sqlite import SqliteConversationRepository

DEMO_QUESTION = "风铃旅人应该怎么配队？"
DEMO_ENTITY = "风铃旅人"
DEMO_REPLACEMENT = "我没有苔羽，能换成谁？"
DEMO_GAP = "演示资料不足"
DEMO_FAILURE = "演示请求失败"
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


class DemoAnswerer:
    """固定虚构回答，仍使用真实的会话领取与提交逻辑。"""

    async def answer(
        self,
        request: QuestionRequest,
        *,
        demo_only: bool,
        generation: GenerationLedger | None = None,
    ) -> dict[str, JsonValue]:
        if request.question == DEMO_GAP:
            return {
                "status": "insufficient_evidence",
                "gaps": ["演示资料缺少适用依据。"],
                "demo_only": True,
                "model_called": False,
            }
        if (
            not request.game_version
            or request.owned_characters is None
            or request.owned_equipment is None
        ):
            return {
                "status": "needs_clarification",
                "questions": ["请确认游戏版本、持有角色和装备。没有持有时可明确选择「确认没有」。"],
                "demo_only": True,
                "model_called": False,
            }
        candidates = [name for name in request.owned_characters if name != DEMO_ENTITY]
        partner = candidates[0] if candidates else None
        advice = (
            f"让风铃旅人负责场上输出，{partner}负责辅助。先完成辅助动作，再切回风铃旅人。"
            if partner
            else "先用风铃旅人练习单角色轮转，等持有名单补充后再选固定队友。"
        )
        equipment = request.owned_equipment
        if equipment:
            advice += f" 优先用手头的「{equipment[0]}」练习轮转。"
        reference: dict[str, JsonValue] = {
            "evidence_id": "fictional-team-2",
            "title": "风铃旅人的队伍笔记",
            "author": "手记演示编者",
            "game_version": request.game_version,
            "section": "第二节 · 配队思路",
            "text": (
                "优先使用已持有的辅助角色。辅助动作完成后，再切回主力。"
                "资源不足时可先练习单角色轮转。"
            ),
            "locators": [{"paragraph": 2}],
        }
        return {
            "status": "answer",
            "demo_only": True,
            "model_called": False,
            "claims": [{"text": advice, "section": "配队建议", "citations": [reference]}],
        }


class Workbench:
    """由单个后台事件循环切换本地存储与显式虚构演示。"""

    def __init__(
        self,
        tool_client_factory: Callable[[ResponsesSettings], ToolModelClient] = ResponsesToolClient,
    ) -> None:
        self._demo = ConversationService(InMemoryConversationRepository(), uuid4(), DemoAnswerer())
        self._local: tuple[SqliteConversationRepository, UUID, KnowledgeReader] | None = None
        self._directory: Path | None = None
        self._tool_client = tool_client_factory

    @property
    def real(self) -> bool:
        return self._local is not None

    async def connect(self, directory: Path) -> Page:
        """打开自带资料和本地会话；不需要任何部署或模型配置。"""
        library = await load_library()
        repository = SqliteConversationRepository(directory / "conversations.sqlite3")
        owner = await repository.initialize()
        items = await repository.list_recent(owner, offset=0, limit=PAGE_SIZE + 1)
        self._local = repository, owner, library
        self._directory = directory
        return Page(items[:PAGE_SIZE], 0, len(items) > PAGE_SIZE)

    async def close(self) -> None:
        self._local = None
        self._directory = None

    def _service(self, model_values: dict[str, str] | None = None) -> ConversationService:
        if self._local is None:
            return self._demo
        repository, owner, library = self._local

        def client() -> ToolModelClient:
            return self._tool_client(ResponsesSettings.from_env(model_values or {}))

        return ConversationService(
            repository,
            owner,
            GuideConversationAnswerer(library, client),
            understanding_factory=lambda: QuestionUnderstandingService(load_catalog()),
        )

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
        self,
        conversation: GuideConversation | None,
        question: str | QuestionPatch,
        *,
        config_path: Path | None = None,
    ) -> SubmitResult:
        if not self.real and question == DEMO_FAILURE:
            raise DesktopError("demo_failure")
        model_values = None
        if self._directory is not None:
            path = config_path if config_path is not None else self._directory / ".env"
            model_values = (await asyncio.to_thread(load_config, path)).values
        service = self._service(model_values)
        if conversation is None:
            assert isinstance(question, str)
            request = (
                QuestionRequest(question)
                if self.real
                else QuestionRequest(
                    question,
                    GuideKind.CHARACTER,
                    entity=DEMO_ENTITY,
                    keywords=("配队",),
                    goal="配队",
                )
            )
            conversation = await service.create(request, demo_only=not self.real)
            assert conversation is not None
        value = question
        if not self.real and isinstance(question, str):
            fields: dict[str, JsonValue] = {"question": question}
            if question == DEMO_REPLACEMENT:
                inventory = conversation.context.owned_characters
                if inventory is not None:
                    fields["owned_characters"] = [name for name in inventory if name != "苔羽"]
            value = QuestionPatch(fields)
        submission = ConversationSubmission()
        try:
            result = await service.ask(
                conversation.id,
                value,
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
