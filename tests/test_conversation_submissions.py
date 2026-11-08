"""直接通过会话 interface 验证自然语言提交；不依赖 CLI 接线或数据库。"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationService,
    ConversationSubmission,
    ConversationTurn,
    QuestionPatch,
)
from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.model_ports import ModelError, ModelResponse, ModelUsage
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.application.question_understanding import (
    QuestionUnderstandingService,
    UnderstandingCancelled,
)
from teyvatnotes.domain.guides import GuideKind
from teyvatnotes.knowledge.conversation_cli import GuideConversationAnswerer
from teyvatnotes.knowledge.demo import PROJECT_ROOT, create_demo_repository
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.knowledge.terminology import load_catalog
from teyvatnotes.knowledge.understanding import JsonUnderstandingDecoder
from teyvatnotes.repositories.conversation_memory import InMemoryConversationRepository

TEXT = "薇斯纳应该怎么配队"
CONTEXT = QuestionRequest(
    "PRIVATE_OLD_QUESTION",
    GuideKind.CHARACTER,
    "7.1",
    "薇斯纳",
    ("配队",),
    goal="配队",
    owned_characters=("薇斯纳", "砂糖"),
    owned_equipment=("蝶变",),
)


@pytest.fixture
def submission_rig():
    value = SimpleNamespace(
        repo=InMemoryConversationRepository(),
        owner=uuid4(),
        factory_calls=0,
        understanding_factories=0,
        model_calls=0,
        answer_calls=0,
        answer_ledgers=[],
        hook=None,
        factory_error=False,
        output=json.dumps(
            {
                "status": "extracted",
                "segments": [
                    {
                        "start": 0,
                        "end": len(TEXT),
                        "quote": TEXT,
                        "updates": [
                            {"field": "entity", "value": "薇斯纳"},
                            {"field": "guide_kind", "value": "character"},
                            {"field": "goal", "value": "配队"},
                            {"field": "keywords", "value": ["配队"]},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )

    class Model:
        async def generate(self, request):
            value.model_calls += 1
            if value.hook:
                await value.hook()
            return ModelResponse(value.output, "fake-extractor", ModelUsage(12, 4))

    def configured():
        value.factory_calls += 1
        if value.factory_error:
            raise ModelError("model_key_not_configured")
        return Model()

    def understanding_factory():
        value.understanding_factories += 1
        return QuestionUnderstandingService(
            load_catalog(), model_factory=configured, decoder=JsonUnderstandingDecoder()
        )

    class Answerer:
        async def answer(self, request, *, demo_only, generation=None):
            value.answer_calls += 1
            value.answer_ledgers.append(generation)
            if generation is not None:
                index = generation.claim("answer")
                generation.record(index, ModelUsage(7, 3), "fake-answer")
            return {
                "status": "answer",
                "claims": [],
                "model_called": True,
                "generation_attempts": 1,
                "input_tokens": 7,
            }

    value.answerer, value.model_factory = Answerer(), configured
    value.service = ConversationService(
        value.repo, value.owner, value.answerer, understanding_factory=understanding_factory
    )
    return value


def test_natural_create_only_extracts_and_ask_uses_same_ledger(submission_rig):
    async def scenario():
        rig = submission_rig
        creation = ConversationSubmission(GenerationLedger())
        partial = await rig.service.create(TEXT, submission=creation)
        assert partial.revision == 0 and not partial.turns
        assert creation.understanding.status == "needs_clarification"
        assert creation.generation.usage()["generation_attempts"] == 1
        assert rig.answer_calls == 0

        state = await rig.service.create(CONTEXT)
        submission = ConversationSubmission(GenerationLedger())
        result = await rig.service.ask(state.id, TEXT, expected_revision=0, submission=submission)
        assert result.revision == 2 and submission.understanding.status == "ready"
        assert rig.answer_ledgers == [submission.generation]
        assert result.latest_reply["generation_attempts"] == 2
        assert result.latest_reply["input_tokens"] == 19
        assert result.latest_reply["round_usage"]["answer"]["generation_attempts"] == 1

    asyncio.run(scenario())


def test_clarification_saves_extraction_usage_without_answer(submission_rig):
    async def scenario():
        rig = submission_rig
        state = await rig.service.create(replace(CONTEXT, owned_equipment=None))
        submission = ConversationSubmission(GenerationLedger())
        result = await rig.service.ask(state.id, TEXT, expected_revision=0, submission=submission)
        reply = result.latest_reply
        assert result.revision == 2 and reply["status"] == "needs_clarification"
        assert reply["generation_attempts"] == 1 and reply["model_called"] is True
        assert reply["input_tokens"] == 12 and rig.answer_calls == 0
        assert reply["round_usage"]["understanding"]["generation_attempts"] == 1
        assert reply["round_usage"]["answer"]["generation_attempts"] == 0
        assert (await rig.service.get(state.id)).latest_reply == reply

    asyncio.run(scenario())


@pytest.mark.parametrize("with_submission", [False, True])
def test_model_submission_automatically_shares_budget_when_omitted(submission_rig, with_submission):
    async def scenario():
        rig = submission_rig
        state = await rig.service.create(CONTEXT)
        submission = ConversationSubmission() if with_submission else None
        result = await rig.service.ask(state.id, TEXT, expected_revision=0, submission=submission)
        ledger = rig.answer_ledgers[0]
        assert isinstance(ledger, GenerationLedger)
        assert ledger.remaining_calls == 2
        assert result.latest_reply["round_usage"]["understanding"]["generation_attempts"] == 1
        assert result.latest_reply["round_usage"]["answer"]["generation_attempts"] == 1
        if submission is not None:
            assert submission.generation is ledger

    asyncio.run(scenario())


def test_race_after_extraction_keeps_metadata_without_replay(submission_rig):
    async def scenario():
        rig = submission_rig
        state = await rig.service.create(CONTEXT)

        async def race():
            await rig.service.close(state.id, expected_revision=0)

        rig.hook = race
        submission = ConversationSubmission(GenerationLedger())
        with pytest.raises(ConversationError, match="^conversation_conflict$"):
            await rig.service.ask(state.id, TEXT, expected_revision=0, submission=submission)
        assert rig.model_calls == 1 and rig.answer_calls == 0
        assert submission.understanding.status == "ready"
        assert submission.generation.usage()["input_tokens"] == 12
        stored = await rig.service.get(state.id)
        assert stored.revision == 1 and stored.closed and not stored.turns
        assert stored.context == CONTEXT

    asyncio.run(scenario())


@pytest.mark.parametrize("state_kind", ["stale", "closed", "busy", "full"])
def test_uneditable_submission_stops_before_understanding_factory(submission_rig, state_kind):
    async def scenario():
        rig = submission_rig
        state = await rig.service.create(CONTEXT)
        if state_kind == "closed":
            state = await rig.service.close(state.id, expected_revision=0)
        elif state_kind == "busy":
            now = datetime.now(UTC)
            state = replace(
                state,
                revision=1,
                turns=(ConversationTurn(uuid4(), 1, CONTEXT, now),),
                updated_at=now,
            )
            await rig.repo.compare_and_swap(state, 0)
        elif state_kind == "full":
            for _ in range(20):
                state = await rig.service.ask(
                    state.id,
                    QuestionPatch({"question": "补充"}),
                    expected_revision=state.revision,
                    clarification=("请补充",),
                )
        submission = ConversationSubmission(GenerationLedger())
        revision = 7 if state_kind == "stale" else state.revision
        with pytest.raises(ConversationError):
            await rig.service.ask(state.id, TEXT, expected_revision=revision, submission=submission)
        assert rig.understanding_factories == rig.factory_calls == rig.answer_calls == 0
        assert submission.understanding is None
        assert submission.generation.usage()["generation_attempts"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("bad_configuration", [False, True])
def test_extraction_failure_never_falls_back_or_claims(submission_rig, bad_configuration):
    async def scenario():
        rig = submission_rig
        state = await rig.service.create(CONTEXT)
        rig.factory_error = bad_configuration
        if not bad_configuration:
            rig.output = '{"status":"extracted","segments":[],"extra":"PRIVATE"}'
        submission = ConversationSubmission(GenerationLedger())
        assert (
            await rig.service.ask(state.id, TEXT, expected_revision=0, submission=submission)
            is None
        )
        assert submission.understanding.status == "error" and rig.answer_calls == 0
        assert rig.model_calls == (0 if bad_configuration else 1)
        if bad_configuration:
            assert submission.understanding.reason == "model_key_not_configured"
        assert "PRIVATE" not in json.dumps(submission.understanding.to_json())
        assert (await rig.service.get(state.id)).revision == 0

    asyncio.run(scenario())


def test_concurrent_submissions_keep_independent_usage_and_understanding(submission_rig):
    async def scenario():
        rig = submission_rig
        first = await rig.service.create(CONTEXT)
        second = await rig.service.create(replace(CONTEXT, owned_equipment=None))
        entered = asyncio.Event()

        async def barrier():
            if rig.model_calls == 2:
                entered.set()
            await entered.wait()

        rig.hook = barrier
        left, right = (ConversationSubmission(GenerationLedger()) for _ in range(2))
        results = await asyncio.wait_for(
            asyncio.gather(
                rig.service.ask(first.id, TEXT, expected_revision=0, submission=left),
                rig.service.ask(second.id, TEXT, expected_revision=0, submission=right),
            ),
            timeout=3,
        )
        assert left.understanding.status == "ready"
        assert right.understanding.status == "needs_clarification"
        assert left.understanding is not right.understanding
        assert left.generation.usage()["generation_attempts"] == 2
        assert right.generation.usage()["generation_attempts"] == 1
        assert [value.latest_reply["status"] for value in results] == [
            "answer",
            "needs_clarification",
        ]
        assert rig.answer_ledgers == [left.generation]

    asyncio.run(scenario())


def test_completion_failure_keeps_submission_usage_and_pending_turn(submission_rig, monkeypatch):
    async def scenario():
        rig = submission_rig
        state = await rig.service.create(CONTEXT)
        commit = rig.repo.compare_and_swap

        async def fail_completion(value, revision):
            if value.active_turn is None:
                raise ConversationError("conversation_storage_unavailable")
            await commit(value, revision)

        monkeypatch.setattr(rig.repo, "compare_and_swap", fail_completion)
        submission = ConversationSubmission(GenerationLedger())
        with pytest.raises(ConversationError, match="^conversation_storage_unavailable$"):
            await rig.service.ask(state.id, TEXT, expected_revision=0, submission=submission)
        assert submission.understanding.status == "ready"
        assert submission.generation.usage()["generation_attempts"] == 2
        assert (await rig.service.get(state.id)).active_turn is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("with_budget", [False, True])
def test_extraction_cancellation_keeps_usage_without_claiming(submission_rig, with_budget):
    async def scenario():
        rig = submission_rig
        state = await rig.service.create(CONTEXT)
        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Event().wait()

        rig.hook = block
        submission = ConversationSubmission(GenerationLedger() if with_budget else None)
        task = asyncio.create_task(
            rig.service.ask(state.id, TEXT, expected_revision=0, submission=submission)
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(UnderstandingCancelled) as caught:
            await task
        assert caught.value.result.generation_attempts == 1
        assert submission.generation.usage()["generation_attempts"] == 1
        assert (await rig.service.get(state.id)).revision == 0 and rig.answer_calls == 0

    asyncio.run(scenario())


def test_rules_do_not_add_shared_usage_or_erase_answer_statistics(submission_rig):
    async def scenario():
        rig = submission_rig
        service = ConversationService(
            rig.repo,
            rig.owner,
            rig.answerer,
            understanding_factory=lambda: QuestionUnderstandingService(load_catalog()),
        )
        state = await service.create(CONTEXT)
        submission = ConversationSubmission()
        result = await service.ask(state.id, TEXT, expected_revision=0, submission=submission)
        assert submission.understanding.generation_attempts == 0
        assert submission.generation is None
        assert result.latest_reply["generation_attempts"] == 1
        assert result.latest_reply["input_tokens"] == 7
        assert "round_usage" not in result.latest_reply
        assert rig.model_calls == 0 and rig.answer_ledgers == [None]

    asyncio.run(scenario())


def test_real_answer_adapter_forwards_budget_into_actual_tool_loop():
    async def scenario():
        reader = await create_demo_repository()
        request = load_question(PROJECT_ROOT / "samples/questions/character.json")
        ledger = GenerationLedger()
        index = ledger.claim("understanding")
        ledger.record(index, ModelUsage(12, 4), "fake-extractor")
        result = await GuideConversationAnswerer(reader).answer(
            request, demo_only=True, generation=ledger
        )
        assert result["status"] == "answer"
        assert ledger.usage("answer")["generation_attempts"] == 3
        assert ledger.remaining_calls == 0

    asyncio.run(scenario())
