"""离线故障注入演示：检查点复用、预算、取消和元数据事件；不恢复付费请求。"""

import asyncio
from dataclasses import replace

from teyvatnotes.application.answer_events import AnswerEventRecorder
from teyvatnotes.application.guide_agent import (
    AgentBudget,
    AgentResult,
    GuideAgentService,
)
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.model_ports import ModelUsage
from teyvatnotes.application.question_preparation import QuestionPreparationService
from teyvatnotes.application.tool_model_ports import ToolTurnRequest, ToolTurnResponse
from teyvatnotes.domain.guides import (
    EvidenceBlock,
    GuideChunk,
    GuideImportError,
    GuideSnapshot,
    IngestionCheckpoint,
    StepName,
    StepStatus,
)
from teyvatnotes.domain.task import JsonValue
from teyvatnotes.knowledge.agent_cli import OfflineToolClient, tool_session
from teyvatnotes.knowledge.answers import JsonAnswerDecoder
from teyvatnotes.knowledge.demo import PROJECT_ROOT, create_demo_repository
from teyvatnotes.knowledge.evaluation_cases import CHARACTER
from teyvatnotes.knowledge.parsing import OfflineSnapshotParser
from teyvatnotes.knowledge.snapshots import load_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository


class CountingParser:
    def __init__(self) -> None:
        self.calls = 0

    def parse(self, snapshot: GuideSnapshot) -> tuple[EvidenceBlock, ...]:
        self.calls += 1
        return OfflineSnapshotParser().parse(snapshot)


class FailOnceCheckpointRepository(InMemoryKnowledgeRepository):
    """只在新的临时内存仓储中注入提交失败，不触碰业务资料。"""

    fail = True

    async def commit_checkpoint(
        self,
        checkpoint: IngestionCheckpoint,
        *,
        chunks: tuple[GuideChunk, ...] = (),
    ) -> IngestionCheckpoint:
        if (
            self.fail
            and checkpoint.name is StepName.CHUNK
            and checkpoint.status is StepStatus.COMPLETED
        ):
            self.fail = False
            raise GuideImportError("simulated_checkpoint_failure")
        return await super().commit_checkpoint(checkpoint, chunks=chunks)


async def checkpoint_probe() -> dict[str, JsonValue]:
    parser, repo = CountingParser(), FailOnceCheckpointRepository()
    snapshot = await asyncio.to_thread(
        load_snapshot, PROJECT_ROOT / "samples/guides/retrieval_character.json"
    )
    error: str | None = None
    try:
        await GuideIngestionService(
            repo, parser=parser, parser_version="reliability-v1"
        ).import_snapshot(snapshot)
    except GuideImportError as exc:
        error = exc.code
    recovered = await GuideIngestionService(
        repo, parser=parser, parser_version="reliability-v1"
    ).import_snapshot(snapshot)
    reused = await GuideIngestionService(
        repo, parser=parser, parser_version="reliability-v1"
    ).import_snapshot(snapshot)
    passed = (
        error == "simulated_checkpoint_failure"
        and parser.calls == 1
        and recovered.reused_steps == (StepName.PARSE,)
        and reused.reused_steps == tuple(StepName)
        and recovered.chunk_count == reused.chunk_count == 3
    )
    return {
        "name": "checkpoint_resume",
        "passed": passed,
        "parse_calls": parser.calls,
        "recovered_steps": [step.value for step in recovered.reused_steps],
        "third_import_reused": [step.value for step in reused.reused_steps],
        "error": error,
    }


class ProbeClient(OfflineToolClient):
    """有界本地模拟，不读取凭据；故障模式不由模型选择。"""

    def __init__(self, mode: str) -> None:
        self.mode, self.calls = mode, 0
        self.entered = asyncio.Event()

    async def generate_turn(self, request: ToolTurnRequest) -> ToolTurnResponse:
        self.calls += 1
        if self.mode == "blocking":
            self.entered.set()
            await asyncio.Event().wait()
        response = self._call(
            f"probe_{self.calls}", "search_knowledge", {"keywords": ["装备"], "limit": 5}
        )
        if self.mode == "tokens":
            return replace(response, usage=ModelUsage(200, 0))
        if self.mode == "context":
            opaque: dict[str, JsonValue] = {
                "type": "reasoning",
                "encrypted_content": "private-demo" * 5000,
            }
            return replace(
                response,
                continuation=(opaque,) + response.continuation,
            )
        return response


async def probe_service(client: OfflineToolClient, budget: AgentBudget) -> GuideAgentService:
    repo = await create_demo_repository()
    return GuideAgentService(
        QuestionPreparationService(repo, demo_only=True),
        lambda: client,
        lambda request: tool_session(repo, request, True),
        JsonAnswerDecoder(),
        budget=budget,
        demo_only=True,
    )


def event_rows(recorder: AnswerEventRecorder) -> list[JsonValue]:
    return [
        {
            "sequence": event.sequence,
            "kind": event.kind,
            "index": event.index,
            "status": event.status,
            "error": event.error,
        }
        for event in recorder.events
    ]


def probe_row(
    name: str, result: AgentResult, expected: str, models: int, tools: int
) -> dict[str, JsonValue]:
    return {
        "name": name,
        "passed": result.answer.reason == expected
        and len(result.model_turns) == models
        and len(result.tool_calls) == tools,
        "error": result.answer.reason,
        "model_attempts": len(result.model_turns),
        "tool_attempts": len(result.tool_calls),
        "model_called": False,
        "cost": None,
    }


async def cancellation_probe() -> dict[str, JsonValue]:
    client, events = ProbeClient("blocking"), AnswerEventRecorder()
    service = await probe_service(client, AgentBudget())
    task = asyncio.create_task(service.answer(CHARACTER, events=events))
    cancelled = False
    try:
        await asyncio.wait_for(client.entered.wait(), 3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return {
        "name": "cooperative_cancellation",
        "passed": cancelled and client.calls == 1 and events.events[-1].kind == "answer_cancelled",
        "attempts": client.calls,
        "events": event_rows(events),
        "provider_stop_or_zero_cost_proven": False,
    }


async def run_reliability() -> dict[str, JsonValue]:
    checks: list[JsonValue] = [await checkpoint_probe()]
    for name, mode, budget, expected, models, tools in (
        (
            "model_budget",
            "loop",
            AgentBudget(max_model_calls=2),
            "agent_model_budget_exceeded",
            2,
            1,
        ),
        ("tool_budget", "loop", AgentBudget(max_tool_calls=1), "agent_tool_budget_exceeded", 2, 1),
        (
            "reported_token_budget",
            "tokens",
            AgentBudget(max_reported_tokens=100),
            "agent_token_budget_exceeded",
            1,
            0,
        ),
        ("unknown_usage_still_bounded", "loop", AgentBudget(), "agent_tool_budget_exceeded", 4, 3),
        ("context_byte_budget", "context", AgentBudget(), "agent_context_budget_exceeded", 1, 1),
        (
            "model_timeout",
            "blocking",
            AgentBudget(total_timeout_seconds=0.05),
            "agent_timeout",
            1,
            0,
        ),
    ):
        client, events = ProbeClient(mode), AnswerEventRecorder()
        result = await (await probe_service(client, budget)).answer(CHARACTER, events=events)
        row = probe_row(name, result, expected, models, tools)
        row["events"] = event_rows(events)
        checks.append(row)
    checks.append(await cancellation_probe())
    events = AnswerEventRecorder()
    answer = await (await probe_service(OfflineToolClient(), AgentBudget())).answer(
        CHARACTER, events=events
    )
    checks.append(
        {
            "name": "answer_metadata_events",
            "passed": answer.answer.status == "answer"
            and len(events.events) == 12
            and [event.sequence for event in events.events] == list(range(1, 13))
            and events.events[-1].kind == "answer_finished",
            "events": event_rows(events),
        }
    )
    return {
        "status": "ok"
        if all(isinstance(row, dict) and row.get("passed") is True for row in checks)
        else "failed",
        "demo_only": True,
        "model_called": False,
        "checks": checks,
        "database_verified": False,
        "cost": None,
        "warnings": [
            "memory_checkpoint_only",
            "no_paid_api",
            "metadata_events_not_text_stream",
            "not_task_worker_step_resume",
            "not_exactly_once_billing",
            "postgres_validation_deferred",
        ],
    }
