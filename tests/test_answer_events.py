"""私有元数据事件的顺序、配对、取消、故障脱敏与并发隔离。"""

import asyncio
from dataclasses import asdict

import pytest

from teyvatnotes.application.answer_events import AnswerEventRecorder
from teyvatnotes.application.guide_agent import AgentBudget
from teyvatnotes.application.model_ports import ModelError
from teyvatnotes.application.tool_model_ports import ToolTurnRequest, ToolTurnResponse
from teyvatnotes.knowledge.agent_cli import OfflineToolClient, agent_to_json
from teyvatnotes.knowledge.evaluation_cases import CHARACTER
from teyvatnotes.knowledge.reliability import ProbeClient, probe_service


def test_events_are_opt_in_and_existing_cli_contract_is_unchanged() -> None:
    async def scenario() -> None:
        service = await probe_service(OfflineToolClient(), AgentBudget())
        before = await service.answer(CHARACTER)
        assert before.events == () and "events" not in agent_to_json(before, CHARACTER)
        recorder = AnswerEventRecorder()
        result = await service.answer(CHARACTER, events=recorder)
        assert result.answer.status == "answer" and result.events == recorder.events
        assert [event.sequence for event in result.events] == list(range(1, 13))
        assert [event.kind for event in result.events] == [
            "answer_started",
            "model_started",
            "model_finished",
            "tool_started",
            "tool_finished",
            "model_started",
            "model_finished",
            "tool_started",
            "tool_finished",
            "model_started",
            "model_finished",
            "answer_finished",
        ]
        assert set(asdict(result.events[0])) == {"sequence", "kind", "index", "status", "error"}
        assert "示例" not in str(result.events) and "reasoning" not in str(result.events)
        assert "events" not in agent_to_json(result, CHARACTER)
        with pytest.raises(ModelError, match="answer_event_session_reused"):
            await service.answer(CHARACTER, events=recorder)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind,index,status",
    [
        ([], None, None),
        ({}, None, None),
        (True, None, None),
        ("unknown", None, None),
        ("model_started", True, None),
        ("model_started", 0, None),
        ("model_started", 2, None),
        ("model_started", 1, []),
        ("model_started", 1, {}),
        ("answer_finished", None, True),
        ("answer_finished", None, "unknown"),
        ("answer_finished", None, None),
        ("model_finished", 1, None),
        ("tool_finished", 1, None),
    ],
)
def test_event_invalid_dynamic_fields_have_stable_errors(kind, index, status) -> None:
    recorder = AnswerEventRecorder()
    recorder.start()
    with pytest.raises(ModelError, match="invalid_answer_event"):
        recorder.record(kind, index=index, status=status)
    assert len(recorder.events) == 1


def test_event_pairing_and_terminal_state_cannot_be_bypassed() -> None:
    recorder = AnswerEventRecorder()
    with pytest.raises(ModelError, match="invalid_answer_event"):
        recorder.record("model_started", index=1)
    recorder.start()
    recorder.record("model_started", index=1)
    for kind, index, status in (
        ("tool_started", 1, None),
        ("model_finished", 2, None),
        ("answer_finished", None, "answer"),
        ("answer_cancelled", None, None),
    ):
        with pytest.raises(ModelError, match="invalid_answer_event"):
            recorder.record(kind, index=index, status=status)
    recorder.record("model_finished", index=1, error="PRIVATE https://secret.invalid")
    assert recorder.events[-1].error is None
    recorder.record("answer_finished", status="error", error="agent_unavailable")
    with pytest.raises(ModelError, match="answer_event_budget_exceeded"):
        recorder.record("model_started", index=2)


def test_event_operation_budgets_remain_bounded() -> None:
    recorder = AnswerEventRecorder()
    recorder.start()
    for index in range(1, 5):
        recorder.record("model_started", index=index)
        recorder.record("model_finished", index=index)
    for index in range(1, 4):
        recorder.record("tool_started", index=index)
        recorder.record("tool_finished", index=index)
    with pytest.raises(ModelError, match="invalid_answer_event"):
        recorder.record("tool_started", index=4)
    recorder.record("answer_finished", status="error")
    assert len(recorder.events) == 16


def test_cancellation_propagates_with_a_single_terminal_event() -> None:
    async def scenario() -> None:
        client, recorder = ProbeClient("blocking"), AnswerEventRecorder()
        service = await probe_service(client, AgentBudget())
        task = asyncio.create_task(service.answer(CHARACTER, events=recorder))
        await asyncio.wait_for(client.entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [event.kind for event in recorder.events] == [
            "answer_started",
            "model_started",
            "model_finished",
            "answer_cancelled",
        ]
        assert recorder.events[-2].error == "model_cancelled" and client.calls == 1

    asyncio.run(scenario())


def test_unexpected_model_failure_is_paired_and_never_leaks_exception_text() -> None:
    class Failing(OfflineToolClient):
        async def generate_turn(self, request: ToolTurnRequest) -> ToolTurnResponse:
            raise RuntimeError("PRIVATE")

    async def scenario() -> None:
        recorder = AnswerEventRecorder()
        service = await probe_service(Failing(), AgentBudget())
        with pytest.raises(RuntimeError, match="PRIVATE"):
            await service.answer(CHARACTER, events=recorder)
        assert recorder.events[-2].error == "model_unavailable"
        assert (
            recorder.events[-1].status == "error"
            and recorder.events[-1].error == "agent_unavailable"
        )
        assert "PRIVATE" not in str(recorder.events)

    asyncio.run(scenario())


def test_concurrent_answers_do_not_share_event_sequences() -> None:
    async def scenario() -> None:
        service = await probe_service(OfflineToolClient(), AgentBudget())
        recorders = [AnswerEventRecorder(), AnswerEventRecorder()]
        results = await asyncio.gather(*(service.answer(CHARACTER, events=r) for r in recorders))
        assert all(result.answer.status == "answer" for result in results)
        assert all([event.sequence for event in r.events] == list(range(1, 13)) for r in recorders)
        assert recorders[0] is not recorders[1]

    asyncio.run(scenario())
