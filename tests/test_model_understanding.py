"""人工固定候选、来源攻击、取消与失败统计；不使用真实模型。"""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.model_ports import (
    ModelError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.application.question_understanding import (
    QuestionUnderstandingService,
    UnderstandingCancelled,
)
from teyvatnotes.domain.guides import GuideKind
from teyvatnotes.knowledge.terminology import load_catalog
from teyvatnotes.knowledge.understanding import JsonUnderstandingDecoder

QUESTION = "薇斯纳应该怎么配队"
DEFAULT_USAGE = ModelUsage(12, 4)
QUESTION_UPDATES = {
    "entity": "薇斯纳",
    "guide_kind": "character",
    "goal": "配队",
    "keywords": ["配队"],
}
CONTEXT = QuestionRequest(
    "PRIVATE_OLD_QUESTION",
    GuideKind.CHARACTER,
    "7.1",
    "薇斯纳",
    ("配队",),
    goal="配队",
    owned_characters=("薇斯纳", "奥黛塔", "砂糖"),
    owned_equipment=("蝶变",),
)


def plan(text: str, parts: list[tuple[str, dict]]) -> dict:
    """只编码人工标注，不调用被测解析器来反推预期。"""
    cursor = 0
    segments = []
    for quote, updates in parts:
        start = text.index(quote, cursor)
        cursor = start + len(quote)
        segments.append({"start": start, "end": cursor, "quote": quote, "updates": updates})
    return {"status": "extracted", "segments": segments}


def encode_output(output: dict) -> str:
    """人工标注用dict，线协议严格编码为闭合field/value列表。"""
    wire = deepcopy(output)
    for segment in wire.get("segments", []):
        if isinstance(segment.get("updates"), dict):
            segment["updates"] = [{"field": k, "value": v} for k, v in segment["updates"].items()]
    return json.dumps(wire, ensure_ascii=False)


class FakeModel:
    def __init__(self, output: str, usage: ModelUsage = DEFAULT_USAGE) -> None:
        self.output, self.usage = output, usage
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(self.output, "fake-extractor", self.usage)


def run_model(
    text: str,
    output: dict | str,
    context: QuestionRequest | None = None,
    *,
    usage: ModelUsage = DEFAULT_USAGE,
    ledger: GenerationLedger | None = None,
):
    client = FakeModel(encode_output(output) if isinstance(output, dict) else output, usage)
    service = QuestionUnderstandingService(
        load_catalog(), model_factory=lambda: client, decoder=JsonUnderstandingDecoder()
    )
    return asyncio.run(service.understand(text, context, generation=ledger)), client


def test_model_segments_natural_comma_input_and_aliases() -> None:
    parts = [
        ("请问薇斯纳应该怎么配队", QUESTION_UPDATES),
        ("当前版本是7.1", {"game_version": "7.1"}),
        ("我拥有的角色有薇斯纳、奥萝塔、砂糖", {"owned_characters": ["薇斯纳", "奥萝塔", "砂糖"]}),
        ("已有装备为蝶变", {"owned_equipment": ["蝶变"]}),
    ]
    text = "，".join(part[0] for part in parts) + "？"
    result, client = run_model(text, plan(text, parts))
    assert result.status == "ready" and result.candidate_question.game_version == "7.1"
    assert result.candidate_question.owned_characters == ("薇斯纳", "奥黛塔", "砂糖")
    assert result.candidate_question.owned_equipment == ("蝶变",)
    assert result.candidate_question.question == text
    assert len(client.requests) == 1 and client.requests[0].max_output_tokens == 2048
    assert client.requests[0].response_schema
    assert result.to_json()["input_tokens"] == 12 and result.to_json()["generation_attempts"] == 1
    assert all(s.quote is None or s.quote in text for s in result.field_sources)


def test_missing_conditions_still_cost_one_extraction_but_no_answer() -> None:
    ledger = GenerationLedger()
    result, _ = run_model(QUESTION, plan(QUESTION, [(QUESTION, QUESTION_UPDATES)]), ledger=ledger)
    assert set(result.missing_fields) == {"game_version", "owned_equipment", "owned_characters"}
    assert result.status == "needs_clarification"
    assert ledger.usage("understanding")["generation_attempts"] == 1
    assert ledger.usage("answer")["generation_attempts"] == 0
    assert ledger.to_json()["model_called"] is True and ledger.to_json()["cost"] is None


def test_only_confirmed_structure_sent_not_old_question_or_answers() -> None:
    text = "她如何配队"
    result, client = run_model(text, plan(text, [(text, QUESTION_UPDATES)]), CONTEXT)
    payload = json.loads(client.requests[0].messages[-1].content)
    assert "PRIVATE_OLD_QUESTION" not in client.requests[0].messages[-1].content
    assert "question" not in payload["confirmed_context"]
    assert set(payload) == {"text", "confirmed_context", "extraction_schema"}
    assert result.status == "ready"


@pytest.mark.parametrize(
    "problem",
    [
        "negative_start",
        "boolean_start",
        "past_end",
        "wrong_quote",
        "overlap",
        "reversed",
        "drop_prefix",
        "drop_suffix",
        "empty",
        "invent_version",
        "partial_keys",
        "conditions",
        "question",
        "extra_key",
    ],
)
def test_source_and_candidate_attacks_never_produce_patch(problem: str) -> None:
    text = QUESTION
    output = plan(text, [(text, QUESTION_UPDATES)])
    segment = output["segments"][0]
    if problem == "negative_start":
        segment["start"] = -1
    elif problem == "boolean_start":
        segment["start"] = True
    elif problem == "past_end":
        segment["end"] += 1
    elif problem == "wrong_quote":
        segment["quote"] = "砂糖应该怎么配队"
    elif problem == "overlap":
        output["segments"].append(dict(segment))
    elif problem == "reversed":
        text = QUESTION + "，版本：7.1"
        output = plan(text, [(QUESTION, QUESTION_UPDATES), ("版本：7.1", {"game_version": "7.1"})])
        output["segments"].reverse()
    elif problem == "drop_prefix":
        text = "不要查询" + QUESTION
        segment["start"] += 4
        segment["end"] += 4
    elif problem == "drop_suffix":
        text += "，但别使用这个角色"
    elif problem == "empty":
        output["segments"] = []
    elif problem == "invent_version":
        segment["updates"] = {**QUESTION_UPDATES, "game_version": "7.1"}
    elif problem == "partial_keys":
        segment["updates"] = {"entity": "薇斯纳"}
    elif problem == "conditions":
        segment["updates"] = {**QUESTION_UPDATES, "conditions": {}}
    elif problem == "question":
        segment["updates"] = {**QUESTION_UPDATES, "question": "改写"}
    else:
        output["untrusted"] = "PRIVATE"
    result, _ = run_model(text, output)
    assert result.status == "error" and result.proposed_updates is None
    assert result.generation_attempts == 1 and result.usage == ModelUsage(12, 4)


@pytest.mark.parametrize(
    "output",
    [
        "{",
        "[]",
        '{"status":"unsupported","status":"extracted","segments":[]}',
        '{"status":"extracted","segments":[{"start":0,"end":1,"quote":"x","updates":{"goal":NaN}}]}',
        '{"status":"unsupported","segments":[],"extra":1}',
        '{"status":"unknown","segments":[]}',
        "字" * 6000,
    ],
    ids=["broken", "array", "duplicate", "nan", "extra", "status", "utf8_bytes"],
)
def test_bad_json_and_utf8_byte_budget_preserve_usage(output: str) -> None:
    result, _ = run_model(QUESTION, output)
    assert result.status == "error" and result.reason == "invalid_understanding_output"
    assert result.to_json()["input_tokens"] == 12 and result.generation_attempts == 1


def test_duplicate_nested_update_keys_are_rejected() -> None:
    raw = encode_output(plan(QUESTION, [(QUESTION, QUESTION_UPDATES)]))
    raw = raw.replace('"field": "goal"', '"field": "goal", "field": "entity"')
    assert run_model(QUESTION, raw)[0].reason == "invalid_understanding_output"


@pytest.mark.parametrize("status", ["needs_clarification", "unsupported"])
def test_model_refusal_is_not_automatic_rule_fallback(status: str) -> None:
    result, client = run_model(QUESTION, {"status": status, "segments": []})
    assert result.status == status and result.proposed_updates is None
    assert len(client.requests) == 1


def test_each_segment_valid_does_not_override_whole_round_conflicts() -> None:
    parts = [("版本：7.1", {"game_version": "7.1"}), ("版本：8.0", {"game_version": "8.0"})]
    text = "，".join(q for q, _ in parts)
    result, _ = run_model(text, plan(text, parts))
    assert result.status == "needs_clarification" and result.proposed_updates is None


def test_negative_inventory_is_complete_replacement_or_rejected() -> None:
    text = "我没有奥萝塔"
    valid = plan(text, [(text, {"owned_characters": ["薇斯纳", "砂糖"]})])
    assert run_model(text, valid, CONTEXT)[0].candidate_question.owned_characters == (
        "薇斯纳",
        "砂糖",
    )
    invalid = plan(text, [(text, {"owned_characters": []})])
    assert run_model(text, invalid, CONTEXT)[0].proposed_updates is None
    assert (
        run_model(text, valid, replace(CONTEXT, owned_characters=None))[0].proposed_updates is None
    )
    assert (
        run_model(text, valid, replace(CONTEXT, conditions=(("队友", "奥黛塔"),)))[
            0
        ].proposed_updates
        is None
    )


@pytest.mark.parametrize(
    "text,updates,expected",
    [
        ("角色列表：无", {"owned_characters": []}, ()),
        ("角色列表：未知", {"owned_characters": None}, None),
    ],
)
def test_model_null_and_empty_distinction(text: str, updates: dict, expected: tuple | None) -> None:
    result, _ = run_model(text, plan(text, [(text, updates)]), CONTEXT)
    assert result.candidate_question.owned_characters == expected


def test_configuration_failure_and_invalid_input_never_claim_attempt() -> None:
    def forbidden():
        raise ModelError("model_key_not_configured")

    service = QuestionUnderstandingService(
        load_catalog(), model_factory=forbidden, decoder=JsonUnderstandingDecoder()
    )
    result = asyncio.run(service.understand(QUESTION))
    assert result.reason == "model_key_not_configured" and result.generation_attempts == 0
    assert asyncio.run(service.understand(" ")).reason == "invalid_understanding_input"


def test_invalid_usage_is_not_echoed() -> None:
    result, _ = run_model(
        QUESTION, plan(QUESTION, [(QUESTION, QUESTION_UPDATES)]), usage=ModelUsage(-1, 2)
    )
    assert result.reason == "invalid_generation_usage" and result.usage == ModelUsage(None, None)


def test_cancellation_keeps_attempt_and_cleanup_and_propagates() -> None:
    async def scenario() -> None:
        entered, cleaned = asyncio.Event(), asyncio.Event()

        class BlockingModel:
            async def generate(self, request: ModelRequest) -> ModelResponse:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                raise AssertionError

        ledger = GenerationLedger()
        service = QuestionUnderstandingService(
            load_catalog(), model_factory=BlockingModel, decoder=JsonUnderstandingDecoder()
        )
        task = asyncio.create_task(service.understand(QUESTION, generation=ledger))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(UnderstandingCancelled) as caught:
            await task
        assert caught.value.result.generation_attempts == 1
        assert caught.value.result.usage == ModelUsage(None, None)
        assert cleaned.is_set() and ledger.to_json()["generation_attempts"] == 1
        assert ledger.to_json()["input_tokens"] is None

    asyncio.run(scenario())


def test_timeout_stops_model_and_retains_unknown_usage() -> None:
    async def scenario() -> None:
        class BlockingModel:
            async def generate(self, request: ModelRequest) -> ModelResponse:
                await asyncio.Event().wait()
                raise AssertionError

        ledger = GenerationLedger(timeout_seconds=0.5)
        service = QuestionUnderstandingService(
            load_catalog(), model_factory=BlockingModel, decoder=JsonUnderstandingDecoder()
        )
        result = await service.understand(QUESTION, generation=ledger)
        assert result.reason == "understanding_timeout" and result.generation_attempts == 1
        assert ledger.to_json()["input_tokens"] is None

    asyncio.run(scenario())


def test_full_context_budget_rejects_before_loading_credentials() -> None:
    calls = []

    def forbidden():
        calls.append(True)
        raise AssertionError("不能读取凭据")

    context = replace(
        CONTEXT,
        owned_characters=tuple("角" * 76 + str(i).zfill(4) for i in range(100)),
        owned_equipment=tuple("装" * 76 + str(i).zfill(4) for i in range(100)),
    )
    service = QuestionUnderstandingService(
        load_catalog(), model_factory=forbidden, decoder=JsonUnderstandingDecoder()
    )
    result = asyncio.run(service.understand(QUESTION, context))
    assert result.reason == "understanding_context_budget_exceeded" and not calls
    assert result.generation_attempts == 0


def test_response_after_shared_deadline_keeps_usage_but_cannot_continue() -> None:
    current = [0.0]

    class SlowClockModel:
        async def generate(self, request):
            current[0] = 2
            return ModelResponse(
                encode_output(plan(QUESTION, [(QUESTION, QUESTION_UPDATES)])),
                "fake-extractor",
                DEFAULT_USAGE,
            )

    ledger = GenerationLedger(timeout_seconds=1, clock=lambda: current[0])
    service = QuestionUnderstandingService(
        load_catalog(), model_factory=SlowClockModel, decoder=JsonUnderstandingDecoder()
    )
    result = asyncio.run(service.understand(QUESTION, generation=ledger))
    assert result.reason == "generation_timeout" and result.proposed_updates is None
    assert result.usage == DEFAULT_USAGE and ledger.remaining_calls == 3


def test_one_service_concurrent_requests_have_independent_statistics() -> None:
    async def scenario():
        client = FakeModel(encode_output(plan(QUESTION, [(QUESTION, QUESTION_UPDATES)])))
        service = QuestionUnderstandingService(
            load_catalog(), model_factory=lambda: client, decoder=JsonUnderstandingDecoder()
        )
        first, second = await asyncio.gather(
            service.understand(QUESTION), service.understand(QUESTION, CONTEXT)
        )
        assert first.status == "needs_clarification" and second.status == "ready"
        assert first.generation_attempts == second.generation_attempts == 1
        assert len(client.requests) == 2

    asyncio.run(scenario())


def test_network_failure_is_sanitized_and_counted_once() -> None:
    class FailingModel:
        async def generate(self, request):
            raise ModelError("PRIVATE_PROVIDER_ERROR / details")

    service = QuestionUnderstandingService(
        load_catalog(), model_factory=FailingModel, decoder=JsonUnderstandingDecoder()
    )
    result = asyncio.run(service.understand(QUESTION))
    assert result.reason == "understanding_unavailable" and result.generation_attempts == 1
    assert "PRIVATE" not in json.dumps(result.to_json())
    assert result.usage == ModelUsage(None, None)


def test_scope_change_requires_new_conversation_even_with_model() -> None:
    text = "版本：8.0"
    result, _ = run_model(text, plan(text, [(text, {"game_version": "8.0"})]), CONTEXT)
    assert result.status == "needs_clarification" and result.reason == "conversation_scope_change"
    assert result.proposed_updates is None and result.questions


def test_model_quest_requires_explicit_location_progress_and_keywords() -> None:
    parts = [
        ("任务：示例任务", {"entity": "示例任务", "guide_kind": "quest"}),
        ("版本：7.1", {"game_version": "7.1"}),
        ("地点：山洞", {"location": "山洞"}),
        ("进度：未开始", {"progress": "未开始"}),
        ("关键词：开门", {"keywords": ["开门"]}),
    ]
    text = "，".join(q for q, _ in parts)
    result, _ = run_model(text, plan(text, parts))
    assert result.status == "ready" and result.candidate_question.guide_kind is GuideKind.QUEST


@pytest.mark.parametrize(
    "problem",
    [
        "old_object",
        "duplicate_field",
        "unknown_field",
        "missing_value",
        "bool_value",
        "number_value",
        "extra_property",
        "wrong_field_type",
    ],
)
def test_closed_wire_updates_reject_invalid_shapes_and_types(problem: str) -> None:
    output = json.loads(encode_output(plan(QUESTION, [(QUESTION, QUESTION_UPDATES)])))
    segment = output["segments"][0]
    first = segment["updates"][0]
    if problem == "old_object":
        segment["updates"] = QUESTION_UPDATES
    elif problem == "duplicate_field":
        segment["updates"].append(dict(first))
    elif problem == "unknown_field":
        first["field"] = "system_command"
    elif problem == "missing_value":
        first.pop("value")
    elif problem == "bool_value":
        first["value"] = True
    elif problem == "number_value":
        first["value"] = 1
    elif problem == "extra_property":
        first["extra"] = "PRIVATE"
    else:
        first["value"] = []
    result, _ = run_model(QUESTION, json.dumps(output, ensure_ascii=False))
    assert result.status == "error" and result.proposed_updates is None
    assert result.generation_attempts == 1


def test_every_schema_object_is_closed_and_all_properties_required() -> None:
    found = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                found.append(value)
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(JsonUnderstandingDecoder().schema())
    assert len(found) == 3
