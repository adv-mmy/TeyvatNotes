"""桌面会话组合根，实际问答仍走现有会话、检索和模型服务。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationService,
    ConversationSubmission,
    ConversationSummary,
    GuideConversation,
    QuestionPatch,
)
from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.application.question_understanding import QuestionUnderstandingService
from teyvatnotes.application.tool_model_ports import ToolModelClient
from teyvatnotes.database import Database, create_database
from teyvatnotes.database.conversations import GuideConversationRecord
from teyvatnotes.database.knowledge import GuideDocumentRecord
from teyvatnotes.database.session import normalize_database_url
from teyvatnotes.desktop.config import DesktopError, load_config
from teyvatnotes.domain.guides import GuideKind
from teyvatnotes.domain.task import JsonValue
from teyvatnotes.knowledge.conversation_cli import GuideConversationAnswerer
from teyvatnotes.knowledge.terminology import load_catalog
from teyvatnotes.models.responses import ResponsesSettings
from teyvatnotes.models.responses_tools import ResponsesToolClient
from teyvatnotes.repositories.conversation_memory import InMemoryConversationRepository
from teyvatnotes.repositories.conversation_postgres import (
    PostgresConversationRepository,
)
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

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
    """由单个后台事件循环拥有连接池与会话操作。"""

    def __init__(
        self,
        tool_client_factory: Callable[[ResponsesSettings], ToolModelClient] = ResponsesToolClient,
    ) -> None:
        self._demo = ConversationService(InMemoryConversationRepository(), uuid4(), DemoAnswerer())
        self._database: Database | None = None
        self._owner: UUID | None = None
        self._path: Path | None = None
        self._tool_client = tool_client_factory

    @property
    def real(self) -> bool:
        return self._database is not None

    async def connect(self, path: Path) -> Page:
        """显式连接选定库，失败时保留原工作区。"""
        config = await asyncio.to_thread(load_config, path)
        try:
            url = normalize_database_url(config.values["DATABASE_URL"])
            owner = UUID(config.values["GUIDE_SESSION_OWNER_ID"])
            if make_url(url).drivername != "postgresql+asyncpg":
                raise ValueError
        except (ArgumentError, KeyError, ValueError):
            raise DesktopError("database_config_required") from None
        database: Database | None = None
        try:
            database = create_database(url)
            async with asyncio.timeout(8):
                async with database.session_factory() as session:
                    await session.execute(select(GuideConversationRecord.id).limit(0))
                    await session.execute(select(GuideDocumentRecord.id).limit(0))
                items = await PostgresConversationRepository(database.session_factory).list_recent(
                    owner, offset=0, limit=PAGE_SIZE + 1
                )
        except asyncio.CancelledError:
            if database is not None:
                await database.dispose()
            raise
        except Exception:
            if database is not None:
                await database.dispose()
            raise DesktopError("database_unavailable") from None
        await self.close()
        self._database, self._owner, self._path = database, owner, path
        return Page(items[:PAGE_SIZE], 0, len(items) > PAGE_SIZE)

    async def close(self) -> None:
        database, self._database = self._database, None
        self._owner, self._path = None, None
        if database is not None:
            await database.dispose()

    def _service(self, model_values: dict[str, str] | None = None) -> ConversationService:
        if self._database is None:
            return self._demo
        assert self._owner is not None
        sessions = self._database.session_factory

        def client() -> ToolModelClient:
            return self._tool_client(ResponsesSettings.from_env(model_values or {}))

        return ConversationService(
            PostgresConversationRepository(sessions),
            self._owner,
            GuideConversationAnswerer(PostgresKnowledgeRepository(sessions), client),
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
        self, conversation: GuideConversation | None, question: str | QuestionPatch
    ) -> SubmitResult:
        if not self.real and question == DEMO_FAILURE:
            raise DesktopError("demo_failure")
        model_values = (
            (await asyncio.to_thread(load_config, self._path)).values if self._path else None
        )
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
