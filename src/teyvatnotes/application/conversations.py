"""私有攻略会话：显式条件、不可混用的范围、修订竞争与有界历史。"""

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID, uuid4

from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.model_ports import ModelError
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.domain.guides import GuideImportError, GuideKind
from teyvatnotes.domain.json_types import JsonValue

if TYPE_CHECKING:
    from teyvatnotes.application.question_understanding import (
        QuestionUnderstandingService,
        UnderstandingResult,
    )

MAX_TURNS = 20
MAX_REPLY_BYTES = 65_536
QUESTION_FIELDS = frozenset(
    {
        "question",
        "guide_kind",
        "game_version",
        "entity",
        "keywords",
        "conditions",
        "goal",
        "location",
        "progress",
        "owned_equipment",
        "owned_characters",
    }
)
REPLY_FIELDS = frozenset(
    {
        "status",
        "claims",
        "questions",
        "gaps",
        "conflicts",
        "reason",
        "model_called",
        "generation_attempts",
        "tool_attempts",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cost",
        "elapsed_ms",
        "warnings",
        "demo_only",
        "round_usage",
    }
)
TurnStatus = Literal["pending", "completed", "error", "cancelled", "interrupted"]


class ConversationError(Exception):
    """只有稳定代码，不携带玩家条件、凭据或底层错误正文。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def question_to_data(request: QuestionRequest) -> dict[str, JsonValue]:
    """显式转换当前玩家状态；保留未知null与明确没有的空列表。"""
    return {
        "question": request.question,
        "guide_kind": request.guide_kind.value if request.guide_kind else None,
        "game_version": request.game_version,
        "entity": request.entity,
        "keywords": list(request.keywords),
        "conditions": dict(request.conditions),
        "goal": request.goal,
        "location": request.location,
        "progress": request.progress,
        "owned_equipment": list(request.owned_equipment)
        if request.owned_equipment is not None
        else None,
        "owned_characters": list(request.owned_characters)
        if request.owned_characters is not None
        else None,
    }


def _optional_text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ConversationError("invalid_conversation_input")
    return value


def _items(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConversationError("invalid_conversation_input")
    return tuple(value)


def question_from_data(value: object) -> QuestionRequest:
    """纯Python边界校验；不依赖Pydantic、数据库或具体模型平台。"""
    if not isinstance(value, dict) or not value.keys() <= QUESTION_FIELDS:
        raise ConversationError("invalid_conversation_input")
    question = value.get("question")
    if not isinstance(question, str):
        raise ConversationError("invalid_conversation_input")
    conditions = value.get("conditions", {})
    if not isinstance(conditions, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in conditions.items()
    ):
        raise ConversationError("invalid_conversation_input")
    kind = value.get("guide_kind")
    if kind is not None and not isinstance(kind, str):
        raise ConversationError("invalid_conversation_input")
    try:
        return QuestionRequest(
            question,
            GuideKind(kind) if kind is not None else None,
            _optional_text(value.get("game_version")),
            _optional_text(value.get("entity")),
            _items(value.get("keywords", [])),
            tuple(sorted(conditions.items())),
            _optional_text(value.get("goal")),
            _optional_text(value.get("location")),
            _optional_text(value.get("progress")),
            _items(value["owned_equipment"]) if value.get("owned_equipment") is not None else None,
            _items(value["owned_characters"])
            if value.get("owned_characters") is not None
            else None,
        )
    except (ValueError, GuideImportError):
        raise ConversationError("invalid_conversation_input") from None


@dataclass(frozen=True, slots=True)
class QuestionPatch:
    """每轮问题必填；省略字段保留，提供列表/条件映射则整项替换。"""

    values: dict[str, JsonValue]

    def __post_init__(self) -> None:
        question_from_data(self.values)
        try:
            size = len(json.dumps(self.values, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ConversationError("invalid_conversation_input") from None
        if size > 16_384:
            raise ConversationError("conversation_input_too_large")
        object.__setattr__(self, "values", deepcopy(self.values))


def merge_question(previous: QuestionRequest, patch: QuestionPatch) -> QuestionRequest:
    """首次补全后锁定版本、实体与类型；改变范围必须新建会话。"""
    checked = QuestionPatch(patch.values)
    before = question_to_data(previous)
    for name in ("game_version", "entity", "guide_kind"):
        known = before[name] is not None
        if name == "game_version" and isinstance(before[name], str):
            known = str(before[name]).strip().lower() not in {"unknown", "未知"}
        if known and name in checked.values and checked.values[name] != before[name]:
            raise ConversationError("conversation_scope_change")
    return question_from_data({**before, **checked.values})


def safe_reply(value: object) -> dict[str, JsonValue]:
    """仅保存受控结构化答案；不保存模型opaque历史或原始工具参数。"""
    if not isinstance(value, dict):
        raise ConversationError("invalid_conversation_reply")
    reply = {key: deepcopy(item) for key, item in value.items() if key in REPLY_FIELDS}
    if not isinstance(reply.get("status"), str) or reply.get("status") not in {
        "answer",
        "needs_clarification",
        "insufficient_evidence",
        "error",
    }:
        raise ConversationError("invalid_conversation_reply")
    try:
        size = len(json.dumps(reply, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ConversationError("invalid_conversation_reply") from None
    if size > MAX_REPLY_BYTES:
        raise ConversationError("conversation_reply_too_large")
    return reply


def utc_now() -> datetime:
    return datetime.now(UTC)


def _valid_date(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """每轮保留当时的完整条件与经过校验的答案，而不是共享可变玩家档案。"""

    id: UUID
    number: int
    request: QuestionRequest
    started_at: datetime
    status: TurnStatus = "pending"
    finished_at: datetime | None = None
    reply: dict[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class GuideConversation:
    """私有会话应用模型；owner是存储隔离标识，不是公开API鉴权凭据。"""

    id: UUID
    owner_id: UUID
    context: QuestionRequest
    demo_only: bool = False
    revision: int = 0
    closed: bool = False
    turns: tuple[ConversationTurn, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @property
    def active_turn(self) -> ConversationTurn | None:
        return self.turns[-1] if self.turns and self.turns[-1].status == "pending" else None

    @property
    def latest_reply(self) -> dict[str, JsonValue] | None:
        """新轮pending时不展示过时答案为当前建议。"""
        return deepcopy(self.turns[-1].reply) if self.turns and self.active_turn is None else None


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """侧栏列表的轻量视图。"""

    id: UUID
    title: str
    game_version: str | None
    updated_at: datetime


def validate_conversation(value: GuideConversation) -> None:
    """仓储入口重验结构、时间与轮次，拒绝绕过类型标注的非法对象。"""
    if (
        not isinstance(value, GuideConversation)
        or not isinstance(value.id, UUID)
        or not isinstance(value.owner_id, UUID)
        or type(value.revision) is not int
        or value.revision < 0
        or type(value.demo_only) is not bool
        or type(value.closed) is not bool
        or not isinstance(value.turns, tuple)
        or len(value.turns) > MAX_TURNS
    ):
        raise ConversationError("invalid_conversation_state")
    if not isinstance(value.context, QuestionRequest) or not all(
        _valid_date(date) for date in (value.created_at, value.updated_at)
    ):
        raise ConversationError("invalid_conversation_state")
    question_from_data(question_to_data(replace(value.context)))
    dates = [value.created_at, value.updated_at]
    if value.updated_at < value.created_at:
        raise ConversationError("invalid_conversation_state")
    ids: set[UUID] = set()
    for number, turn in enumerate(value.turns, 1):
        if (
            not isinstance(turn, ConversationTurn)
            or not isinstance(turn.id, UUID)
            or turn.id in ids
            or type(turn.number) is not int
            or turn.number != number
            or not isinstance(turn.status, str)
            or not isinstance(turn.request, QuestionRequest)
            or not _valid_date(turn.started_at)
            or turn.status not in {"pending", "completed", "error", "cancelled", "interrupted"}
        ):
            raise ConversationError("invalid_conversation_state")
        ids.add(turn.id)
        question_from_data(question_to_data(replace(turn.request)))
        dates.append(turn.started_at)
        if turn.status == "pending":
            if (
                turn is not value.turns[-1]
                or turn.reply is not None
                or turn.finished_at is not None
            ):
                raise ConversationError("invalid_conversation_state")
        else:
            if (
                not _valid_date(turn.finished_at)
                or turn.finished_at is None
                or turn.reply is None
                or turn.finished_at < turn.started_at
            ):
                raise ConversationError("invalid_conversation_state")
            dates.append(turn.finished_at)
            if safe_reply(turn.reply) != turn.reply or (
                (turn.status == "completed") == (turn.reply.get("status") == "error")
            ):
                raise ConversationError("invalid_conversation_state")
        if number > 1:
            previous = value.turns[number - 2]
            merge_question(previous.request, QuestionPatch(question_to_data(turn.request)))
            if previous.finished_at is None or turn.started_at < previous.finished_at:
                raise ConversationError("invalid_conversation_state")
    if any(not isinstance(date, datetime) or date.utcoffset() is None for date in dates):
        raise ConversationError("invalid_conversation_state")
    if value.turns and value.context != value.turns[-1].request:
        raise ConversationError("invalid_conversation_state")
    if value.closed and value.active_turn is not None:
        raise ConversationError("invalid_conversation_state")


class ConversationRepository(Protocol):
    async def add(self, conversation: GuideConversation) -> None: ...
    async def get(self, owner_id: UUID, conversation_id: UUID) -> GuideConversation | None: ...
    async def list_recent(
        self, owner_id: UUID, *, offset: int, limit: int
    ) -> tuple[ConversationSummary, ...]: ...
    async def compare_and_swap(
        self, conversation: GuideConversation, expected_revision: int
    ) -> None:
        """完整状态一次提交；竞争失败不覆盖已有修改。"""
        ...


@dataclass(slots=True)
class ConversationSubmission:
    """一次提交独立持有理解结果与共享预算；保存或清理失败后仍可读取实际消耗。"""

    generation: GenerationLedger | None = None
    understanding: "UnderstandingResult | None" = field(default=None, init=False)


class ConversationAnswerer(Protocol):
    async def answer(
        self,
        request: QuestionRequest,
        *,
        demo_only: bool,
        generation: GenerationLedger | None = None,
    ) -> dict[str, JsonValue]:
        """每轮重新创建检索/工具授权；不能信任或续接旧轮模型状态。"""
        ...


class ConversationService:
    """先原子领取新轮，再生成，再原子完成；事务不跨模型网络请求。"""

    def __init__(
        self,
        repository: ConversationRepository,
        owner_id: UUID,
        answerer: ConversationAnswerer,
        *,
        clock: Callable[[], datetime] = utc_now,
        timeout_seconds: float = 100.0,
        understanding_factory: Callable[[], "QuestionUnderstandingService"] | None = None,
    ) -> None:
        if not isinstance(owner_id, UUID):
            raise ConversationError("invalid_conversation_owner")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 120
        ):
            raise ConversationError("invalid_conversation_configuration")
        self._timeout_seconds = timeout_seconds
        self._repository, self._owner_id = repository, owner_id
        self._answerer, self._clock = answerer, clock
        self._understanding_factory = understanding_factory

    async def create(
        self,
        request: QuestionRequest | str,
        *,
        demo_only: bool = False,
        submission: ConversationSubmission | None = None,
    ) -> GuideConversation | None:
        submission = submission if submission is not None else ConversationSubmission()
        if isinstance(request, str):
            understood = await self._understand(request, None, submission)
            if understood.proposed_updates is None:
                return None
            request = question_from_data(understood.proposed_updates.values)
        if not isinstance(request, QuestionRequest):
            raise ConversationError("invalid_conversation_input")
        context = question_from_data(question_to_data(replace(request)))
        now = self._clock()
        value = GuideConversation(
            uuid4(), self._owner_id, context, demo_only, created_at=now, updated_at=now
        )
        validate_conversation(value)
        await self._repository.add(value)
        return deepcopy(value)

    async def get(self, conversation_id: UUID) -> GuideConversation:
        if not isinstance(conversation_id, UUID):
            raise ConversationError("invalid_conversation_id")
        value = await self._repository.get(self._owner_id, conversation_id)
        if value is None:
            raise ConversationError("conversation_not_found")
        validate_conversation(value)
        return value

    async def list_recent(self, *, offset: int, limit: int) -> tuple[ConversationSummary, ...]:
        """按会话所有者查询最近更新的会话摘要。"""
        return await self._repository.list_recent(self._owner_id, offset=offset, limit=limit)

    async def _editable(self, conversation_id: UUID, revision: int) -> GuideConversation:
        value = await self.get(conversation_id)
        if type(revision) is not int or revision != value.revision:
            raise ConversationError("conversation_conflict")
        if value.closed:
            raise ConversationError("conversation_closed")
        return value

    async def _askable(self, conversation_id: UUID, revision: int) -> GuideConversation:
        value = await self._editable(conversation_id, revision)
        if value.active_turn is not None:
            raise ConversationError("conversation_busy")
        if len(value.turns) >= MAX_TURNS:
            raise ConversationError("conversation_turn_budget_exceeded")
        return value

    async def _understand(
        self,
        text: str,
        context: QuestionRequest | None,
        submission: ConversationSubmission,
    ) -> "UnderstandingResult":
        if self._understanding_factory is None:
            raise ConversationError("understanding_not_configured")
        generation = submission.generation
        created_generation = generation is None
        if generation is None:
            generation = GenerationLedger()
            submission.generation = generation
        try:
            # 工厂在会话预检后执行；陈旧请求连词表都不加载，更不会读取模型凭据。
            result = await self._understanding_factory().understand(
                text, context, generation=generation
            )
            submission.understanding = result
            return result
        finally:
            # 规则零提取不新增整轮统计；已发生的模型尝试（含取消）继续与回答共用账本。
            if created_generation and generation.usage()["generation_attempts"] == 0:
                submission.generation = None

    @staticmethod
    def _validate_clarification(clarification: tuple[str, ...]) -> None:
        if (
            not isinstance(clarification, tuple)
            or len(clarification) > 20
            or any(not isinstance(q, str) or not 1 <= len(q) <= 400 for q in clarification)
        ):
            raise ConversationError("invalid_conversation_input")

    async def ask(
        self,
        conversation_id: UUID,
        patch: QuestionPatch | str,
        *,
        expected_revision: int,
        clarification: tuple[str, ...] = (),
        submission: ConversationSubmission | None = None,
    ) -> GuideConversation | None:
        # 理解层缺条件时只保存明示补充与追问，不进入检索/模型生成。
        self._validate_clarification(clarification)
        before = await self._askable(conversation_id, expected_revision)
        submission = submission if submission is not None else ConversationSubmission()
        if isinstance(patch, str):
            understood = await self._understand(patch, before.context, submission)
            if understood.proposed_updates is None:
                return None
            patch = understood.proposed_updates
            clarification = (
                understood.questions if understood.status == "needs_clarification" else ()
            )
            self._validate_clarification(clarification)
        generation = submission.generation
        request = merge_question(before.context, patch)
        turn = ConversationTurn(uuid4(), len(before.turns) + 1, request, self._clock())
        started = replace(
            before,
            context=request,
            turns=(*before.turns, turn),
            revision=before.revision + 1,
            updated_at=turn.started_at,
        )
        await self._repository.compare_and_swap(started, before.revision)
        if clarification:
            return await self._finish(
                started,
                "completed",
                self._with_usage(
                    {
                        "status": "needs_clarification",
                        "questions": list(clarification),
                        "model_called": False,
                        "generation_attempts": 0,
                    },
                    generation,
                ),
            )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                reply = safe_reply(
                    await self._answerer.answer(
                        request, demo_only=started.demo_only, generation=generation
                    )
                )
        except asyncio.CancelledError:
            # 不吞取消；存储失败后遗留pending须显式处理中断。
            with suppress(ConversationError):
                await self._finish(
                    started,
                    "cancelled",
                    self._with_usage(
                        {"status": "error", "reason": "conversation_cancelled"}, generation
                    ),
                )
            raise
        except TimeoutError:
            reply = {"status": "error", "reason": "conversation_timeout"}
        except (ConversationError, GuideImportError, ModelError) as exc:
            code = (
                exc.code
                if re.fullmatch(r"[a-z_]{1,64}", exc.code)
                else "conversation_generation_failed"
            )
            reply = {"status": "error", "reason": code}
        except Exception:
            reply = {"status": "error", "reason": "conversation_generation_failed"}
        return await self._finish(
            started,
            "error" if reply["status"] == "error" else "completed",
            self._with_usage(reply, generation),
        )

    @staticmethod
    def _with_usage(
        reply: dict[str, JsonValue], generation: GenerationLedger | None
    ) -> dict[str, JsonValue]:
        if generation is None:
            return reply
        # 顶层统计为整轮，分阶段另存，避免追问已调用提取却误报零调用。
        usage = generation.to_json()
        return {
            **reply,
            **generation.usage(),
            "elapsed_ms": usage["elapsed_ms"],
            "round_usage": usage,
        }

    async def _finish(
        self,
        started: GuideConversation,
        status: TurnStatus,
        reply: dict[str, JsonValue],
    ) -> GuideConversation:
        turn = replace(
            started.turns[-1], status=status, finished_at=self._clock(), reply=safe_reply(reply)
        )
        finished = replace(
            started,
            turns=(*started.turns[:-1], turn),
            revision=started.revision + 1,
            updated_at=turn.finished_at or self._clock(),
        )
        # 人工中断后的旧请求不能用自己的迟到结果覆盖新轮。
        await self._repository.compare_and_swap(finished, started.revision)
        return finished

    async def close(self, conversation_id: UUID, *, expected_revision: int) -> GuideConversation:
        value = await self._editable(conversation_id, expected_revision)
        if value.active_turn is not None:
            raise ConversationError("conversation_busy")
        closed = replace(value, closed=True, revision=value.revision + 1, updated_at=self._clock())
        await self._repository.compare_and_swap(closed, value.revision)
        return closed

    async def interrupt(
        self, conversation_id: UUID, *, expected_revision: int
    ) -> GuideConversation:
        """显式记录失联轮次，不重放请求、不宣称停止服务商计算或计费。"""
        value = await self._editable(conversation_id, expected_revision)
        if value.active_turn is None:
            raise ConversationError("conversation_not_busy")
        return await self._finish(
            value,
            "interrupted",
            {"status": "error", "reason": "conversation_interrupted"},
        )
