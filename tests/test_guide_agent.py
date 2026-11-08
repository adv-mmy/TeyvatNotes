"""最小循环的预算、证据授权、失败遥测、取消和并发隔离；无真实模型调用。"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from teyvatnotes.application.guide_agent import AgentBudget, GuideAgentService
from teyvatnotes.application.model_ports import ModelError, ModelUsage
from teyvatnotes.application.question_preparation import QuestionPreparationService
from teyvatnotes.application.tool_model_ports import ToolTurnRequest, ToolTurnResponse
from teyvatnotes.knowledge.agent_cli import (
    OfflineToolClient,
    agent_to_json,
    main,
    tool_session,
)
from teyvatnotes.knowledge.answers import JsonAnswerDecoder
from teyvatnotes.knowledge.demo import create_demo_repository
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.knowledge.tools import KnowledgeToolSession

ROOT = Path(__file__).resolve().parents[1]


class FakeToolClient(OfflineToolClient):
    def __init__(self, problem: str | None = None) -> None:
        self.calls = 0
        self.problem = problem

    async def generate_turn(self, request: ToolTurnRequest) -> ToolTurnResponse:
        self.calls += 1
        if self.problem == "model_failure" and self.calls == 2:
            raise ModelError("model_unavailable")
        if self.problem in {"loop", "unknown"}:
            name = "forbidden_secret_name" if self.problem == "unknown" else "search_knowledge"
            response = self._call(f"call_{self.calls}", name, {"keywords": ["装备"], "limit": 5})
        else:
            response = await super().generate_turn(request)
        if response.text and self.problem in {"citation", "equipment", "order"}:
            body = json.loads(response.text)
            if self.problem == "citation":
                body["claims"][0]["evidence_ids"] = [str(uuid4())]
            elif self.problem == "equipment":
                body["claims"][0]["required_equipment"] = ["未拥有的装备"]
            else:
                body["claims"].reverse()
            response = replace(response, text=json.dumps(body, ensure_ascii=False))
        if self.problem == "no_read":
            response = replace(
                response,
                text=json.dumps(
                    {
                        "status": "answer",
                        "claims": [
                            {
                                "text": "猜测",
                                "kind": "fact",
                                "section": "summary",
                                "evidence_ids": [str(uuid4())],
                                "required_equipment": [],
                                "required_characters": [],
                            }
                        ],
                        "questions": [],
                        "gaps": [],
                        "conflicts": [],
                    }
                ),
                calls=(),
                continuation=(),
            )
        if self.problem == "duplicate" and self.calls == 2:
            response = replace(response, calls=(replace(response.calls[0], call_id="demo_search"),))
        if self.problem == "multiple" and response.calls:
            response = replace(response, calls=response.calls * 2)
        if self.problem == "context" and response.calls:
            response = replace(
                response,
                continuation=({"type": "reasoning", "encrypted_content": "x" * 48_000},)
                + response.continuation,
            )
        return replace(
            response,
            model="fake-tool-model",
            usage=ModelUsage(30_000 if self.problem == "tokens" else 10, 5),
        )


async def run_answer(
    problem: str | None = None, name: str = "character", budget: AgentBudget | None = None
):
    reader = await create_demo_repository()
    request = load_question(ROOT / f"samples/questions/{name}.json")
    client = FakeToolClient(problem)
    service = GuideAgentService(
        QuestionPreparationService(reader, demo_only=True),
        lambda: client,
        lambda question: tool_session(reader, question, True),
        JsonAnswerDecoder(),
        budget=budget,
        demo_only=True,
    )
    return await service.answer(request), request, client


@pytest.mark.parametrize("name", ["character", "quest"])
def test_search_read_answer_and_per_turn_telemetry(name: str) -> None:
    async def scenario() -> None:
        result, request, client = await run_answer(name=name)
        public = agent_to_json(result, request)
        assert result.answer.status == "answer" and client.calls == 3
        assert [call.name for call in result.tool_calls] == ["search_knowledge", "read_evidence"]
        assert public["generation_attempts"] == 3 and public["tool_attempts"] == 2
        assert public["input_tokens"] == 30 and public["output_tokens"] == 15
        assert public["model_called"] is False and public["cost"] is None
        assert public["claims"][0]["citations"]
        assert all(turn.elapsed_ms >= 0 for turn in result.model_turns)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"game_version": None}, "needs_clarification"),
        ({"owned_equipment": None}, "needs_clarification"),
        ({"keywords": ("无命中",)}, "insufficient_evidence"),
    ],
)
def test_preflight_does_not_create_client_or_tool_session(changes, status: str) -> None:
    async def scenario() -> None:
        reader = await create_demo_repository()
        request = replace(load_question(ROOT / "samples/questions/character.json"), **changes)

        def forbidden(*args):
            raise AssertionError("未就绪不能创建模型或工具会话")

        result = await GuideAgentService(
            QuestionPreparationService(reader, demo_only=True),
            forbidden,
            forbidden,
            JsonAnswerDecoder(),
        ).answer(request)
        assert result.answer.status == status and not result.model_turns and not result.tool_calls

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "problem,reason,calls",
    [
        ("citation", "invalid_answer_citation", 3),
        ("equipment", "answer_equipment_not_owned", 3),
        ("no_read", "agent_evidence_not_read", 1),
        ("duplicate", "duplicate_tool_call_id", 2),
        ("multiple", "invalid_tool_turn", 1),
        ("tokens", "agent_token_budget_exceeded", 1),
        ("context", "agent_context_budget_exceeded", 1),
        ("model_failure", "model_unavailable", 2),
        ("loop", "agent_tool_budget_exceeded", 4),
        ("unknown", "agent_tool_budget_exceeded", 4),
    ],
)
def test_failure_is_bounded_without_final_claims(problem: str, reason: str, calls: int) -> None:
    async def scenario() -> None:
        result, request, client = await run_answer(problem)
        assert result.answer.status == "error" and result.answer.reason == reason
        assert client.calls == calls and not result.answer.claims
        public = agent_to_json(result, request)
        assert public["generation_attempts"] == calls
        assert public["cost"] is None
        if problem == "model_failure":
            assert public["input_tokens"] is None
            assert result.model_turns[0].usage.input_tokens == 10
            assert result.model_turns[1].error == "model_unavailable"
        if problem == "unknown":
            assert all(
                call.name == "unknown"
                and call.parameters is None
                and call.error == "tool_not_allowed"
                for call in result.tool_calls
            )
            assert "forbidden_secret_name" not in json.dumps(public)

    asyncio.run(scenario())


def test_quest_order_still_rejected() -> None:
    result, _, _ = asyncio.run(run_answer("order", "quest"))
    assert result.answer.reason == "invalid_quest_order"


def test_last_model_turn_does_not_execute_useless_tool() -> None:
    result, _, client = asyncio.run(run_answer("loop", budget=AgentBudget(max_model_calls=1)))
    assert client.calls == 1 and not result.tool_calls
    assert result.answer.reason == "agent_model_budget_exceeded"


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_model_calls", 5),
        ("max_tool_calls", True),
        ("max_reported_tokens", 0),
        ("total_timeout_seconds", float("nan")),
        ("tool_timeout_seconds", 11),
    ],
)
def test_budget_cannot_expand_hard_caps(field: str, value: object) -> None:
    with pytest.raises(ModelError, match="invalid_agent_budget"):
        AgentBudget(**{field: value})


def test_exact_token_cap_stops_more_work_but_accepts_final_answer() -> None:
    async def scenario() -> None:
        stopped, _, client = await run_answer(budget=AgentBudget(max_reported_tokens=15))
        assert stopped.answer.reason == "agent_token_budget_exceeded"
        assert client.calls == 1 and not stopped.tool_calls
        accepted, _, client = await run_answer(budget=AgentBudget(max_reported_tokens=45))
        assert accepted.answer.status == "answer" and client.calls == 3

    asyncio.run(scenario())


def test_tool_timeout_total_timeout_and_cancellation_are_recorded() -> None:
    async def scenario(mode: str) -> None:
        reader = await create_demo_repository()
        request = load_question(ROOT / "samples/questions/character.json")
        base = tool_session(reader, request, True)
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        class SlowSession(KnowledgeToolSession):
            async def execute(self, call):
                if call.name != "read_evidence":
                    return await base.execute(call)
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                raise AssertionError("不可到达")

        session = SlowSession(reader, base._scope)
        client = FakeToolClient()
        service = GuideAgentService(
            QuestionPreparationService(reader, demo_only=True),
            lambda: client,
            lambda _: session,
            JsonAnswerDecoder(),
            budget=AgentBudget(
                total_timeout_seconds=0.03 if mode == "total" else 90,
                tool_timeout_seconds=0.01 if mode == "tool" else 10,
            ),
        )
        task = asyncio.create_task(service.answer(request))
        await entered.wait()
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client.calls == 2
        else:
            result = await task
            if mode == "total":
                assert result.answer.reason == "agent_timeout" and client.calls == 2
                assert result.tool_calls[-1].error == "tool_interrupted"
            else:
                assert result.answer.status == "insufficient_evidence" and client.calls == 3
                assert result.tool_calls[-1].error == "tool_timeout"
            assert len(result.tool_calls) == 2
        assert cleaned.is_set()

    for mode in ("tool", "total", "cancel"):
        asyncio.run(scenario(mode))


class BlockingClient:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleaned = False
        self.calls = 0

    async def generate_turn(self, request: ToolTurnRequest) -> ToolTurnResponse:
        self.calls += 1
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaned = True
        raise AssertionError("不可到达")


def test_model_cancellation_and_total_timeout_stop_followups() -> None:
    async def scenario(cancel: bool) -> None:
        reader = await create_demo_repository()
        request = load_question(ROOT / "samples/questions/character.json")
        client = BlockingClient()
        service = GuideAgentService(
            QuestionPreparationService(reader, demo_only=True),
            lambda: client,
            lambda q: tool_session(reader, q, True),
            JsonAnswerDecoder(),
            budget=AgentBudget(total_timeout_seconds=0.03 if not cancel else 90),
        )
        task = asyncio.create_task(service.answer(request))
        await client.entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.answer.reason == "agent_timeout" and len(result.model_turns) == 1
            assert result.model_turns[0].error == "model_cancelled"
        assert client.cleaned and client.calls == 1

    asyncio.run(scenario(True))
    asyncio.run(scenario(False))


def test_same_service_concurrent_requests_use_independent_sessions() -> None:
    async def scenario() -> None:
        reader = await create_demo_repository()
        service = GuideAgentService(
            QuestionPreparationService(reader, demo_only=True),
            FakeToolClient,
            lambda q: tool_session(reader, q, True),
            JsonAnswerDecoder(),
            demo_only=True,
        )
        results = await asyncio.gather(
            *(
                service.answer(load_question(ROOT / f"samples/questions/{name}.json"))
                for name in ("quest", "character")
            )
        )
        assert all(
            result.answer.status == "answer" and len(result.model_turns) == 3 for result in results
        )
        assert {window.hit.document.guide_kind.value for window in results[0].answer.evidence} == {
            "quest"
        }
        assert {window.hit.document.guide_kind.value for window in results[1].answer.evidence} == {
            "character"
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,status,attempts",
    [
        ("character", "answer", 3),
        ("quest", "answer", 3),
        ("clarification", "needs_clarification", 0),
    ],
)
def test_cli_demo_never_reads_credentials_and_emits_trace(
    name, status, attempts, monkeypatch, capsys
) -> None:
    def forbidden():
        raise AssertionError("演示不能读取凭据或访问真实模型")

    monkeypatch.setattr("teyvatnotes.knowledge.agent_cli.configured_tool_client", forbidden)
    assert main([str(ROOT / f"samples/questions/{name}.json"), "--demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == status and result["generation_attempts"] == attempts
    assert result["model_called"] is False and result["demo_only"] is True
    assert result["storage"] == "memory_demo" and result["cost"] is None


def test_cli_requires_business_database_without_fallback(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TEST_DATABASE_URL", "never-print-secret")
    assert main([str(ROOT / "samples/questions/character.json")]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "database_not_configured",
    }
