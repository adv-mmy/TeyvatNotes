"""提取与回答共用调用/时间/已报告Token；模拟循环不发网络请求。"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from teyvatnotes.application.guide_agent import GuideAgentService
from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.model_ports import ModelError, ModelUsage
from teyvatnotes.application.question_preparation import QuestionPreparationService
from teyvatnotes.application.tool_model_ports import ToolTurnRequest, ToolTurnResponse
from teyvatnotes.knowledge.agent_cli import OfflineToolClient, tool_session
from teyvatnotes.knowledge.answers import JsonAnswerDecoder
from teyvatnotes.knowledge.demo import create_demo_repository
from teyvatnotes.knowledge.questions import load_question

ROOT = Path(__file__).resolve().parents[1]


class CountedToolClient(OfflineToolClient):
    def __init__(self) -> None:
        self.calls = 0

    async def generate_turn(self, request: ToolTurnRequest) -> ToolTurnResponse:
        self.calls += 1
        return replace(await super().generate_turn(request), usage=ModelUsage(7, 3))


async def answer(ledger: GenerationLedger):
    reader = await create_demo_repository()
    request = load_question(ROOT / "samples/questions/character.json")
    client = CountedToolClient()
    service = GuideAgentService(
        QuestionPreparationService(reader, demo_only=True),
        lambda: client,
        lambda question: tool_session(reader, question, True),
        JsonAnswerDecoder(),
        demo_only=True,
    )
    return await service.answer(request, generation=ledger), client


def extracted(ledger: GenerationLedger) -> None:
    index = ledger.claim("understanding")
    ledger.record(index, ModelUsage(12, 4), "fake-extractor")


def test_one_extraction_leaves_three_calls_for_search_read_answer() -> None:
    ledger = GenerationLedger()
    extracted(ledger)
    result, client = asyncio.run(answer(ledger))
    assert result.answer.status == "answer" and client.calls == 3
    assert ledger.remaining_calls == 0
    total = ledger.to_json()
    assert total["generation_attempts"] == 4
    assert total["input_tokens"] == 33 and total["output_tokens"] == 13
    assert total["understanding"]["generation_attempts"] == 1
    assert total["answer"]["generation_attempts"] == 3
    assert total["cost"] is None


def test_last_shared_call_cannot_execute_tool_without_budget_for_response() -> None:
    ledger = GenerationLedger(max_calls=2)
    extracted(ledger)
    result, client = asyncio.run(answer(ledger))
    assert result.answer.reason == "generation_call_budget_exceeded"
    assert client.calls == 1 and result.tool_calls == ()
    assert ledger.to_json()["generation_attempts"] == 2


def test_shared_reported_tokens_stop_before_next_tool() -> None:
    ledger = GenerationLedger(max_reported_tokens=30)
    extracted(ledger)
    result, client = asyncio.run(answer(ledger))
    assert result.answer.reason == "generation_token_budget_exceeded"
    assert client.calls == 2 and len(result.tool_calls) == 1
    assert ledger.reported_tokens == 36


def test_unknown_usage_not_zero_but_known_component_still_bounded() -> None:
    ledger = GenerationLedger(max_reported_tokens=20)
    ledger.claim("understanding")
    index = ledger.claim("answer")
    ledger.record(index, ModelUsage(20, None), "fake-answer")
    assert ledger.to_json()["input_tokens"] is None
    assert ledger.to_json()["output_tokens"] is None
    assert ledger.reported_tokens == 20
    with pytest.raises(ModelError, match="generation_token_budget_exceeded"):
        ledger.claim("answer")


def test_elapsed_deadline_is_shared_not_restarted_for_answer() -> None:
    current = [0.0]
    ledger = GenerationLedger(clock=lambda: current[0], timeout_seconds=1)
    extracted(ledger)
    current[0] = 2
    result, client = asyncio.run(answer(ledger))
    assert result.answer.reason == "generation_timeout" and client.calls == 0
    assert ledger.to_json()["elapsed_ms"] == 2000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_calls": True},
        {"max_calls": 5},
        {"max_reported_tokens": 0},
        {"max_reported_tokens": 20001},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": True},
        {"timeout_seconds": 91},
    ],
)
def test_budget_configuration_has_runtime_bounds(kwargs: dict) -> None:
    with pytest.raises(ModelError, match="invalid_generation_budget"):
        GenerationLedger(**kwargs)


def test_failed_call_finishes_timing_without_replacing_known_usage() -> None:
    current = [0.0]
    ledger = GenerationLedger(clock=lambda: current[0])
    first = ledger.claim("understanding")
    current[0] = 0.2
    ledger.finish(first)
    assert ledger.usage("understanding")["elapsed_ms"] == 200
    second = ledger.claim("answer")
    current[0] = 0.5
    ledger.record(second, ModelUsage(2, 3), "fake-answer")
    current[0] = 0.9
    ledger.finish(second)
    assert ledger.usage("answer")["elapsed_ms"] == 300
    assert ledger.usage("answer")["input_tokens"] == 2


def test_independent_ledgers_do_not_share_attempts() -> None:
    first, second = GenerationLedger(), GenerationLedger()
    extracted(first)
    assert first.remaining_calls == 3 and second.remaining_calls == 4
