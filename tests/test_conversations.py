"""会话条件、scope锁定、领取前CAS、取消/失联恢复和历史保护。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from teyvatnotes.application.conversations import (
    MAX_REPLY_BYTES,
    MAX_TURNS,
    ConversationError,
    ConversationService,
    ConversationTurn,
    GuideConversation,
    QuestionPatch,
    merge_question,
    safe_reply,
    validate_conversation,
)
from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.model_ports import ModelError
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.domain import JsonValue
from teyvatnotes.domain.guides import GuideKind
from teyvatnotes.repositories.conversation_codec import (
    conversation_from_data,
    conversation_to_data,
)
from teyvatnotes.repositories.conversation_memory import InMemoryConversationRepository

REQUEST = QuestionRequest(
    "怎样培养？",
    GuideKind.CHARACTER,
    "test-version",
    "测试角色",
    ("装备",),
    (("命座", "零"),),
    "装备",
    owned_equipment=("旧装备",),
    owned_characters=("测试角色",),
)


def test_mutated_patch_is_rechecked_before_merge() -> None:
    patch = QuestionPatch({"question": "追问"})
    patch.values["owned_equipment"] = [True]
    with pytest.raises(ConversationError, match="invalid_conversation_input"):
        merge_question(REQUEST, patch)


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        {},
        {"status": True},
        {"status": "unexpected"},
        {"status": "answer", "cost": float("nan")},
    ],
)
def test_reply_invalid_values_are_rejected(reply: object) -> None:
    with pytest.raises(ConversationError, match="invalid_conversation_reply"):
        safe_reply(reply)


def test_reply_byte_limit_not_character_limit() -> None:
    with pytest.raises(ConversationError, match="conversation_reply_too_large"):
        safe_reply({"status": "answer", "claims": ["中" * (MAX_REPLY_BYTES // 2)]})


@pytest.mark.parametrize("timeout", [True, 0, -1, 121, float("nan"), float("inf")])
def test_timeout_configuration_is_strict(timeout: float) -> None:
    with pytest.raises(ConversationError, match="invalid_conversation_configuration"):
        ConversationService(
            InMemoryConversationRepository(), uuid4(), RecordingAnswerer(), timeout_seconds=timeout
        )


def test_timeout_records_terminal_error_and_allows_explicit_next_round() -> None:
    class BlockingAnswerer:
        async def answer(
            self,
            request: QuestionRequest,
            *,
            demo_only: bool,
            generation: GenerationLedger | None = None,
        ) -> dict[str, JsonValue]:
            await asyncio.Event().wait()
            return {"status": "answer"}

    async def scenario() -> None:
        repo, owner = InMemoryConversationRepository(), uuid4()
        service = ConversationService(repo, owner, BlockingAnswerer(), timeout_seconds=1)
        created = await service.create(REQUEST)
        result = await service.ask(
            created.id, QuestionPatch({"question": "问"}), expected_revision=0
        )
        assert result.revision == 2 and result.turns[0].status == "error"
        assert result.latest_reply == {"status": "error", "reason": "conversation_timeout"}
        next_service = ConversationService(repo, owner, RecordingAnswerer())
        next_value = await next_service.ask(
            created.id, QuestionPatch({"question": "再问"}), expected_revision=2
        )
        assert next_value.revision == 4 and next_value.turns[0].status == "error"

    asyncio.run(scenario())


def test_cancellation_with_failed_storage_preserves_pending_for_manual_interrupt() -> None:
    class FailingCompletionRepository(InMemoryConversationRepository):
        fail_completion = True

        async def compare_and_swap(
            self, conversation: GuideConversation, expected_revision: int
        ) -> None:
            if self.fail_completion and conversation.active_turn is None:
                raise ConversationError("conversation_storage_unavailable")
            await super().compare_and_swap(conversation, expected_revision)

    class BlockingAnswerer:
        started = asyncio.Event()

        async def answer(
            self,
            request: QuestionRequest,
            *,
            demo_only: bool,
            generation: GenerationLedger | None = None,
        ) -> dict[str, JsonValue]:
            self.started.set()
            await asyncio.Event().wait()
            return {"status": "answer"}

    async def scenario() -> None:
        repo, responder = FailingCompletionRepository(), BlockingAnswerer()
        service = ConversationService(repo, uuid4(), responder)
        created = await service.create(REQUEST)
        task = asyncio.create_task(
            service.ask(created.id, QuestionPatch({"question": "问"}), expected_revision=0)
        )
        await asyncio.wait_for(responder.started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pending = await service.get(created.id)
        assert pending.revision == 1 and pending.active_turn and pending.latest_reply is None
        repo.fail_completion = False
        finished = await service.interrupt(created.id, expected_revision=1)
        assert finished.revision == 2 and finished.turns[0].status == "interrupted"

    asyncio.run(scenario())


class RecordingAnswerer:
    def __init__(self) -> None:
        self.requests: list[QuestionRequest] = []

    async def answer(
        self,
        request: QuestionRequest,
        *,
        demo_only: bool,
        generation: GenerationLedger | None = None,
    ) -> dict[str, JsonValue]:
        self.requests.append(request)
        return {
            "status": "needs_clarification" if request.owned_equipment is None else "answer",
            "questions": ["请提供装备"] if request.owned_equipment is None else [],
            "claims": [],
            "cost": None,
            "model_called": not demo_only,
            "tool_calls": [{"parameters": "not-persisted"}],
            "reasoning": "not-persisted",
        }


@pytest.mark.parametrize("field", ["guide_kind", "game_version", "entity"])
def test_scope_cannot_change_or_clear_after_confirmation(field: str) -> None:
    for value in (None, "quest" if field == "guide_kind" else "different"):
        with pytest.raises(ConversationError, match="conversation_scope_change"):
            merge_question(REQUEST, QuestionPatch({"question": "追问", field: value}))


def test_patch_omitted_null_empty_and_replacement_are_distinct() -> None:
    kept = merge_question(REQUEST, QuestionPatch({"question": "继续"}))
    assert (
        kept.owned_equipment == REQUEST.owned_equipment
        and kept.game_version == REQUEST.game_version
    )
    empty = merge_question(kept, QuestionPatch({"question": "没有装备", "owned_equipment": []}))
    unknown = merge_question(kept, QuestionPatch({"question": "未知", "owned_equipment": None}))
    changed = merge_question(
        kept,
        QuestionPatch(
            {
                "question": "替换",
                "owned_equipment": ["新装备"],
                "conditions": {"命座": "一"},
            }
        ),
    )
    assert empty.owned_equipment == () and unknown.owned_equipment is None
    assert changed.owned_equipment == ("新装备",) and changed.conditions == (("命座", "一"),)
    assert REQUEST.owned_equipment == ("旧装备",)
    assert (
        merge_question(
            changed, QuestionPatch({"question": "清除条件", "conditions": {}})
        ).conditions
        == ()
    )


def test_first_clarification_binds_unknown_scope_and_patch_copies() -> None:
    initial = QuestionRequest("问", game_version="未知")
    raw: dict[str, JsonValue] = {
        "question": "补充",
        "game_version": "v1",
        "entity": "实体",
        "guide_kind": "quest",
    }
    patch = QuestionPatch(raw)
    raw["entity"] = "mutated"
    result = merge_question(initial, patch)
    assert result.entity == "实体" and result.game_version == "v1"
    with pytest.raises(ConversationError, match="conversation_scope_change"):
        merge_question(result, QuestionPatch({"question": "换版本", "game_version": "v2"}))


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"question": " "},
        {"question": True},
        {"question": "问", "extra": 1},
        {"question": "问", "guide_kind": False},
        {"question": "问", "guide_kind": "other"},
        {"question": "问", "keywords": "text"},
        {"question": "问", "keywords": [1]},
        {"question": "问", "owned_equipment": ["a", "a"]},
        {"question": "问", "owned_equipment": "a"},
        {"question": "问", "goal": 3},
        {"question": "问", "conditions": {"key": None}},
        {"question": "问", "conditions": []},
        {"question": "问", "progress": "x" * 161},
    ],
)
def test_invalid_patch_has_stable_error(raw: dict[str, JsonValue]) -> None:
    with pytest.raises(ConversationError, match="invalid_conversation_input"):
        QuestionPatch(raw)


def test_clarification_then_update_saves_snapshots_and_sanitized_replies() -> None:
    async def scenario() -> None:
        repo, responder = InMemoryConversationRepository(), RecordingAnswerer()
        service = ConversationService(repo, uuid4(), responder)
        created = await service.create(replace(REQUEST, owned_equipment=None), demo_only=True)
        assert created.revision == 0 and created.latest_reply is None and not responder.requests
        first = await service.ask(
            created.id, QuestionPatch({"question": "问"}), expected_revision=0
        )
        assert first.latest_reply and first.latest_reply["status"] == "needs_clarification"
        assert first.revision == 2 and first.turns[0].request.owned_equipment is None
        second = await service.ask(
            first.id,
            QuestionPatch(
                {
                    "question": "已经有这些装备",
                    "owned_equipment": ["新装备"],
                }
            ),
            expected_revision=2,
        )
        assert second.revision == 4 and second.context.owned_equipment == ("新装备",)
        assert responder.requests[0].owned_equipment is None
        assert responder.requests[1].game_version == REQUEST.game_version
        assert all("reasoning" not in (turn.reply or {}) for turn in second.turns)
        assert all("tool_calls" not in (turn.reply or {}) for turn in second.turns)
        assert second.turns[0].reply is not None
        second.turns[0].reply["questions"] = ["mutated"]
        saved = await service.get(created.id)
        assert saved.turns[0].reply is not None
        assert saved.turns[0].reply["questions"] == ["请提供装备"]
        restored = conversation_from_data(conversation_to_data(saved))
        assert restored == saved

    asyncio.run(scenario())


def test_stale_revision_other_owner_closed_and_scope_change_never_call_model() -> None:
    async def scenario() -> None:
        repo, responder = InMemoryConversationRepository(), RecordingAnswerer()
        service = ConversationService(repo, uuid4(), responder)
        value = await service.create(REQUEST)
        other = ConversationService(repo, uuid4(), responder)
        with pytest.raises(ConversationError, match="conversation_not_found"):
            await other.get(value.id)
        with pytest.raises(ConversationError, match="conversation_conflict"):
            await service.ask(value.id, QuestionPatch({"question": "问"}), expected_revision=True)
        with pytest.raises(ConversationError, match="conversation_scope_change"):
            await service.ask(
                value.id, QuestionPatch({"question": "问", "entity": "other"}), expected_revision=0
            )
        closed = await service.close(value.id, expected_revision=0)
        with pytest.raises(ConversationError, match="conversation_closed"):
            await service.ask(
                value.id, QuestionPatch({"question": "问"}), expected_revision=closed.revision
            )
        assert not responder.requests

    asyncio.run(scenario())


def test_concurrent_claim_only_winner_calls_model() -> None:
    async def scenario() -> None:
        class BarrierRepository(InMemoryConversationRepository):
            def __init__(self) -> None:
                super().__init__()
                self.reads = 0
                self.ready = asyncio.Event()

            async def get(self, owner_id: UUID, conversation_id: UUID) -> GuideConversation | None:
                value = await super().get(owner_id, conversation_id)
                self.reads += 1
                if self.reads == 2:
                    self.ready.set()
                await self.ready.wait()
                return value

        repo, responder = BarrierRepository(), RecordingAnswerer()
        service = ConversationService(repo, uuid4(), responder)
        value = await service.create(REQUEST)
        results = await asyncio.gather(
            *[
                service.ask(value.id, QuestionPatch({"question": "问"}), expected_revision=0)
                for _ in range(2)
            ],
            return_exceptions=True,
        )
        assert len(responder.requests) == 1
        assert sum(isinstance(result, GuideConversation) for result in results) == 1
        assert sum(isinstance(result, ConversationError) for result in results) == 1

    asyncio.run(scenario())


def test_interrupt_prevents_late_write_without_replaying_and_preserves_latest_context() -> None:
    async def scenario() -> None:
        class Waiting(RecordingAnswerer):
            def __init__(self) -> None:
                super().__init__()
                self.entered, self.release = asyncio.Event(), asyncio.Event()

            async def answer(
                self,
                request: QuestionRequest,
                *,
                demo_only: bool,
                generation: GenerationLedger | None = None,
            ) -> dict[str, JsonValue]:
                if not self.requests:
                    self.requests.append(request)
                    self.entered.set()
                    await self.release.wait()
                    return {"status": "answer", "claims": [{"text": "old"}]}
                return await super().answer(request, demo_only=demo_only, generation=generation)

        repo, responder = InMemoryConversationRepository(), Waiting()
        service = ConversationService(repo, uuid4(), responder)
        initial = await service.create(REQUEST)
        old = asyncio.create_task(
            service.ask(initial.id, QuestionPatch({"question": "old"}), expected_revision=0)
        )
        await responder.entered.wait()
        pending = await service.get(initial.id)
        assert pending.revision == 1 and pending.latest_reply is None
        with pytest.raises(ConversationError, match="conversation_busy"):
            await service.ask(
                initial.id, QuestionPatch({"question": "blocked"}), expected_revision=1
            )
        recovered = await service.interrupt(initial.id, expected_revision=1)
        assert recovered.turns[0].status == "interrupted" and len(responder.requests) == 1
        latest = await service.ask(
            initial.id,
            QuestionPatch(
                {
                    "question": "new",
                    "owned_equipment": ["new"],
                }
            ),
            expected_revision=2,
        )
        responder.release.set()
        with pytest.raises(ConversationError, match="conversation_conflict"):
            await old
        assert (await service.get(initial.id)) == latest
        assert latest.context.owned_equipment == ("new",) and len(responder.requests) == 2

    asyncio.run(scenario())


def test_cancel_propagates_and_finishes_turn_without_retry() -> None:
    async def scenario() -> None:
        class Waiting(RecordingAnswerer):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()

            async def answer(
                self,
                request: QuestionRequest,
                *,
                demo_only: bool,
                generation: GenerationLedger | None = None,
            ) -> dict[str, JsonValue]:
                self.requests.append(request)
                self.entered.set()
                await asyncio.Event().wait()
                raise AssertionError

        repo, responder = InMemoryConversationRepository(), Waiting()
        service = ConversationService(repo, uuid4(), responder)
        value = await service.create(REQUEST)
        task = asyncio.create_task(
            service.ask(value.id, QuestionPatch({"question": "问"}), expected_revision=0)
        )
        await responder.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        saved = await service.get(value.id)
        assert saved.revision == 2 and saved.turns[-1].status == "cancelled"
        assert saved.latest_reply == {"status": "error", "reason": "conversation_cancelled"}
        assert len(responder.requests) == 1

    asyncio.run(scenario())


def test_budget_counts_all_rounds_and_close_remains_available() -> None:
    async def scenario() -> None:
        repo, responder = InMemoryConversationRepository(), RecordingAnswerer()
        service = ConversationService(repo, uuid4(), responder)
        value = await service.create(REQUEST)
        for _ in range(MAX_TURNS):
            value = await service.ask(
                value.id, QuestionPatch({"question": "问"}), expected_revision=value.revision
            )
        with pytest.raises(ConversationError, match="conversation_turn_budget_exceeded"):
            await service.ask(
                value.id, QuestionPatch({"question": "问"}), expected_revision=value.revision
            )
        assert len(responder.requests) == MAX_TURNS
        assert (await service.close(value.id, expected_revision=value.revision)).closed

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("PRIVATE"), ModelError("model_unavailable")])
def test_generation_errors_are_recorded_without_raw_exception_or_retries(error: Exception) -> None:
    async def scenario() -> None:
        class Failing:
            calls = 0

            async def answer(
                self,
                request: QuestionRequest,
                *,
                demo_only: bool,
                generation: GenerationLedger | None = None,
            ) -> dict[str, JsonValue]:
                self.calls += 1
                raise error

        repo, responder = InMemoryConversationRepository(), Failing()
        service = ConversationService(repo, uuid4(), responder)
        value = await service.create(REQUEST)
        result = await service.ask(value.id, QuestionPatch({"question": "问"}), expected_revision=0)
        assert result.turns[-1].status == "error" and responder.calls == 1
        assert "PRIVATE" not in str(result.latest_reply)

    asyncio.run(scenario())


def test_unknown_reply_fields_are_never_serialized_by_direct_repository_inputs() -> None:
    now = datetime.now(UTC)
    turn = ConversationTurn(
        uuid4(), 1, REQUEST, now, "completed", now, {"status": "answer", "reasoning": "PRIVATE"}
    )
    value = GuideConversation(
        uuid4(), uuid4(), REQUEST, turns=(turn,), created_at=now, updated_at=now
    )
    with pytest.raises(ConversationError, match="invalid_conversation_state"):
        conversation_to_data(value)
    assert safe_reply(turn.reply) == {"status": "answer"}


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": True},
        {"demo_only": 1},
        {"created_at": datetime.now()},
        {"updated_at": "PRIVATE"},
        {"context": None},
        {"turns": []},
    ],
)
def test_runtime_state_types_are_checked_before_comparisons(changes: dict[str, object]) -> None:
    value = GuideConversation(uuid4(), uuid4(), REQUEST)
    with pytest.raises(ConversationError, match="invalid_conversation_state"):
        validate_conversation(replace(value, **changes))


def test_codec_corruption_and_round_trip_are_explicit() -> None:
    value = GuideConversation(uuid4(), uuid4(), REQUEST)
    payload = conversation_to_data(value)
    assert conversation_from_data(payload) == value
    for key, bad in [
        ("revision", True),
        ("turns", "bad"),
        ("created_at", "PRIVATE"),
        ("context", {}),
    ]:
        changed = deepcopy(payload)
        changed[key] = bad
        with pytest.raises(ConversationError, match="conversation_storage_corrupt"):
            conversation_from_data(changed)


def test_repository_rejects_duplicate_wrong_owner_and_idle_context_rewrite() -> None:
    async def scenario() -> None:
        repo = InMemoryConversationRepository()
        service = ConversationService(repo, uuid4(), RecordingAnswerer())
        initial = await service.create(REQUEST)
        with pytest.raises(ConversationError, match="conversation_exists"):
            await repo.add(initial)
        with pytest.raises(ConversationError, match="conversation_not_found"):
            await repo.compare_and_swap(replace(initial, owner_id=uuid4(), revision=1), 0)
        with pytest.raises(ConversationError, match="invalid_conversation_transition"):
            await repo.compare_and_swap(
                replace(initial, context=replace(REQUEST, question="未领取轮次"), revision=1), 0
            )
        assert await service.get(initial.id) == initial

    asyncio.run(scenario())


def test_repository_cannot_rewrite_completed_history_when_claiming_next_round() -> None:
    async def scenario() -> None:
        repo = InMemoryConversationRepository()
        service = ConversationService(repo, uuid4(), RecordingAnswerer())
        initial = await service.create(REQUEST)
        completed = await service.ask(
            initial.id, QuestionPatch({"question": "问"}), expected_revision=0
        )
        changed = replace(completed.turns[0], reply={"status": "answer", "claims": ["改写历史"]})
        pending = ConversationTurn(uuid4(), 2, completed.context, completed.updated_at)
        with pytest.raises(ConversationError, match="invalid_conversation_transition"):
            await repo.compare_and_swap(replace(completed, turns=(changed, pending), revision=3), 2)
        assert await service.get(initial.id) == completed

    asyncio.run(scenario())
