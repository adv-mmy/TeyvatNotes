"""第六周 SSE、关联日志、健康检查和指标验收测试。"""

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from conftest import AUTH_HEADERS
from fastapi.testclient import TestClient

from teyvatnotes.api import create_app
from teyvatnotes.api.routes.health import _repository_ready
from teyvatnotes.api.sse import stream_task_events
from teyvatnotes.application import (
    CreateTaskCommand,
    TaskEvent,
    TaskEventService,
    TaskService,
)
from teyvatnotes.domain import (
    JsonValue,
    Task,
    TaskResult,
    TaskStatus,
    TaskType,
)
from teyvatnotes.observability import (
    JsonLogFormatter,
    MetricsRegistry,
    bind_correlation_context,
    configure_logging,
    current_correlation_context,
)
from teyvatnotes.repositories import InMemoryTaskRepository
from teyvatnotes.runtime import RuntimeSettings, TaskExecutionError, TaskRuntime
from teyvatnotes.worker import TaskWorker, WorkerSettings


def _sse_data(body: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def _create_sum(
    client: TestClient,
    *,
    client_request_id: str = "week6-request",
) -> tuple[UUID, str]:
    response = client.post(
        "/v1/tasks",
        headers={**AUTH_HEADERS, "X-Request-ID": client_request_id},
        json={"task_type": "sum_numbers", "payload": {"numbers": [1, 2, 3]}},
    )
    assert response.status_code == 201
    generated_request_id = response.headers["X-Request-ID"]
    assert generated_request_id != client_request_id
    UUID(generated_request_id)
    return UUID(response.json()["id"]), generated_request_id


def test_task_events_include_status_progress_and_correlation() -> None:
    repository = InMemoryTaskRepository()
    application = create_app(repository)

    with TestClient(application) as client:
        task_id, request_id = _create_sum(client)
        response = client.get(f"/v1/tasks/{task_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_data(response.text)
    assert [event["event_type"] for event in events] == [
        "task_created",
        "status_changed",
        "task_progress",
        "task_progress",
        "status_changed",
    ]
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
    assert events[0]["payload"] == {"version": 0, "request_id": request_id}
    assert [
        event["payload"]["progress"] for event in events if event["event_type"] == "task_progress"
    ] == [0, 100]

    status_events = [event for event in events if event["event_type"] == "status_changed"]
    attempt_ids = {event["payload"]["attempt_id"] for event in status_events}
    assert len(attempt_ids) == 1
    assert all(event["payload"]["request_id"] == request_id for event in status_events)


def test_api_request_can_be_traced_to_worker_log() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    runtime_logger = logging.getLogger("teyvatnotes.runtime.manager")
    original_handlers = list(runtime_logger.handlers)
    original_propagate = runtime_logger.propagate
    original_disabled = runtime_logger.disabled
    runtime_logger.handlers = [handler]
    runtime_logger.propagate = False
    runtime_logger.disabled = False
    runtime_logger.setLevel(logging.INFO)
    application = create_app(InMemoryTaskRepository())

    try:
        with TestClient(application) as client:
            task_id, request_id = _create_sum(
                client,
                client_request_id="trace-request-42",
            )
            client.get(f"/v1/tasks/{task_id}/events")
    finally:
        runtime_logger.handlers = original_handlers
        runtime_logger.propagate = original_propagate
        runtime_logger.disabled = original_disabled

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    started = next(record for record in records if record.get("event") == "task_started")
    completed = next(record for record in records if record.get("event") == "task_completed")
    assert started["request_id"] == request_id
    assert started["task_id"] == str(task_id)
    assert UUID(started["attempt_id"])
    assert started["attempt_id"] == completed["attempt_id"]
    assert started["worker_id"].startswith("embedded-")


def test_http_log_uses_route_template_and_omits_sensitive_path_and_query() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    app_logger = logging.getLogger("teyvatnotes.api.app")
    original_handlers = list(app_logger.handlers)
    original_propagate = app_logger.propagate
    original_disabled = app_logger.disabled
    app_logger.handlers = [handler]
    app_logger.propagate = False
    app_logger.disabled = False
    app_logger.setLevel(logging.INFO)
    application = create_app(InMemoryTaskRepository())

    try:
        with TestClient(application) as client:
            response = client.get("/v1/unknown-secret-value?api_key=secret-value")
    finally:
        app_logger.handlers = original_handlers
        app_logger.propagate = original_propagate
        app_logger.disabled = original_disabled

    assert response.status_code == 404
    assert "secret-value" not in stream.getvalue()
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    completed = next(
        record for record in records if record.get("event") == "http_request_completed"
    )
    assert completed["http_path"] == "unmatched"


def test_client_request_id_is_not_copied_to_logs_or_events() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    app_logger = logging.getLogger("teyvatnotes.api.app")
    original_handlers = list(app_logger.handlers)
    original_propagate = app_logger.propagate
    app_logger.handlers = [handler]
    app_logger.propagate = False
    app_logger.setLevel(logging.INFO)
    application = create_app(InMemoryTaskRepository())

    try:
        with TestClient(application) as client:
            task_id, generated_id = _create_sum(
                client,
                client_request_id="password:do-not-log",
            )
            events = _sse_data(client.get(f"/v1/tasks/{task_id}/events").text)
    finally:
        app_logger.handlers = original_handlers
        app_logger.propagate = original_propagate

    assert "do-not-log" not in stream.getvalue()
    assert events[0]["payload"]["request_id"] == generated_id
    assert all(event["payload"].get("request_id") != "password:do-not-log" for event in events)


def test_uvicorn_log_channels_cannot_emit_request_target_or_exception_text() -> None:
    stream = io.StringIO()
    project_logger = logging.getLogger("teyvatnotes")
    error_logger = logging.getLogger("uvicorn.error")
    access_logger = logging.getLogger("uvicorn.access")
    loggers = (project_logger, error_logger, access_logger)
    original = {
        logger.name: (
            list(logger.handlers),
            logger.level,
            logger.propagate,
            logger.disabled,
        )
        for logger in loggers
    }

    try:
        configure_logging()
        assert access_logger.disabled is True
        assert len(error_logger.handlers) == 1
        error_logger.handlers[0].setStream(stream)  # type: ignore[attr-defined]
        access_logger.info('127.0.0.1 - "GET /?api_key=access-secret HTTP/1.1" 404')
        try:
            raise RuntimeError("password=error-secret")
        except RuntimeError:
            error_logger.exception("Exception in ASGI application: password=error-secret")
    finally:
        for logger in loggers:
            handlers, level, propagate, disabled = original[logger.name]
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled

    assert "access-secret" not in stream.getvalue()
    assert "error-secret" not in stream.getvalue()
    record = json.loads(stream.getvalue())
    assert record["logger"] == "uvicorn.error"
    assert record["message"] == "ASGI 服务器事件"
    assert record["exception_type"] == "RuntimeError"


def test_last_event_id_resumes_without_duplicates_or_gaps() -> None:
    repository = InMemoryTaskRepository()
    application = create_app(repository)

    with TestClient(application) as client:
        task_id, _ = _create_sum(client)
        all_events = _sse_data(client.get(f"/v1/tasks/{task_id}/events").text)
        acknowledged_id = int(all_events[2]["id"])
        resumed = client.get(
            f"/v1/tasks/{task_id}/events",
            headers={"Last-Event-ID": str(acknowledged_id)},
        )

    resumed_events = _sse_data(resumed.text)
    assert [event["id"] for event in resumed_events] == [
        event["id"] for event in all_events if int(event["id"]) > acknowledged_id
    ]
    assert all(int(event["id"]) > acknowledged_id for event in resumed_events)


def test_event_endpoint_validates_cursor_and_unknown_task() -> None:
    application = create_app(InMemoryTaskRepository())
    with TestClient(application) as client:
        invalid = client.get(
            f"/v1/tasks/{uuid4()}/events",
            headers={"Last-Event-ID": "-1"},
        )
        missing = client.get(f"/v1/tasks/{uuid4()}/events")

    assert invalid.status_code == 422
    assert invalid.json()["code"] == "validation_error"
    assert missing.status_code == 404
    assert missing.json()["code"] == "task_not_found"


def test_application_composition_requires_or_accepts_explicit_side_ports() -> None:
    class CoreOnlyRepository:
        def __init__(self) -> None:
            self.inner = InMemoryTaskRepository()

        async def add(self, task: Task):
            return await self.inner.add(task)

        async def get(self, task_id: UUID):
            return await self.inner.get(task_id)

        async def list_tasks(
            self,
            *,
            status: TaskStatus | None,
            offset: int,
            limit: int,
        ):
            return await self.inner.list_tasks(status=status, offset=offset, limit=limit)

        async def transition(
            self,
            task_id: UUID,
            target_status: TaskStatus,
            *,
            result: TaskResult | None = None,
            attempt_id: UUID | None = None,
        ):
            return await self.inner.transition(
                task_id,
                target_status,
                result=result,
                attempt_id=attempt_id,
            )

    repository = CoreOnlyRepository()
    with pytest.raises(TypeError, match="TaskEventRepository"):
        create_app(repository)

    application = create_app(
        repository,
        event_repository=repository.inner,
        readiness_probe=repository.inner,
    )
    with TestClient(application) as client:
        task_id, _ = _create_sum(client)
        response = client.get(f"/v1/tasks/{task_id}/events")

    assert response.status_code == 200
    assert _sse_data(response.text)[-1]["to_status"] == "succeeded"


@dataclass(slots=True)
class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@dataclass(slots=True)
class _DisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return True


def test_running_stream_emits_heartbeat_when_idle() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        task = Task(payload={"numbers": [1]})
        await repository.add(task)
        events = await repository.list_events(task.id, after_id=0, limit=10)
        assert events is not None
        generator: AsyncIterator[str] = stream_task_events(
            _ConnectedRequest(),  # type: ignore[arg-type]
            task_id=task.id,
            after_id=events[-1].id,
            event_service=TaskEventService(repository),
            task_service=TaskService(repository),
            heartbeat_seconds=0,
        )
        assert await anext(generator) == ": keep-alive\n\n"
        await generator.aclose()

    asyncio.run(scenario())


def test_running_stream_stops_when_client_disconnects() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        task = Task(payload={"numbers": [1]})
        await repository.add(task)
        events = await repository.list_events(task.id, after_id=0, limit=10)
        assert events is not None
        generator = stream_task_events(
            _DisconnectedRequest(),  # type: ignore[arg-type]
            task_id=task.id,
            after_id=events[-1].id,
            event_service=TaskEventService(repository),
            task_service=TaskService(repository),
        )

        with pytest.raises(StopAsyncIteration):
            await anext(generator)
        assert not {
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith("agent-runtime")
        }

    asyncio.run(scenario())


def test_terminal_race_drains_events_committed_between_list_and_status_read() -> None:
    class TerminalRaceRepository(InMemoryTaskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.armed = False
            self.empty_read_done = False
            self.finish_before_get = False

        async def list_events(
            self,
            task_id: UUID,
            *,
            after_id: int,
            limit: int,
        ) -> tuple[TaskEvent, ...] | None:
            if self.armed and not self.empty_read_done:
                self.empty_read_done = True
                self.finish_before_get = True
                return ()
            return await super().list_events(task_id, after_id=after_id, limit=limit)

        async def get(self, task_id: UUID) -> Task | None:
            if self.finish_before_get:
                self.finish_before_get = False
                await super().transition(task_id, TaskStatus.RUNNING)
                await super().transition(task_id, TaskStatus.SUCCEEDED)
            return await super().get(task_id)

    async def scenario() -> None:
        repository = TerminalRaceRepository()
        task = Task(payload={"numbers": [1]})
        await repository.add(task)
        initial = await repository.list_events(task.id, after_id=0, limit=10)
        assert initial is not None
        repository.armed = True
        generator = stream_task_events(
            _ConnectedRequest(),  # type: ignore[arg-type]
            task_id=task.id,
            after_id=initial[-1].id,
            event_service=TaskEventService(repository),
            task_service=TaskService(repository),
        )
        chunks = [chunk async for chunk in generator]
        events = _sse_data("".join(chunks))
        assert [event["event_type"] for event in events] == [
            "status_changed",
            "task_progress",
            "task_progress",
            "status_changed",
        ]

    asyncio.run(scenario())


def test_health_endpoints_distinguish_liveness_and_readiness() -> None:
    class UnavailableRepository(InMemoryTaskRepository):
        async def is_ready(self) -> bool:
            raise RuntimeError("postgres password=do-not-log")

    application = create_app(UnavailableRepository(), execution_mode="api_only")
    with TestClient(application) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok", "checks": {"process": "ready"}}
    assert ready.status_code == 503
    assert ready.json()["code"] == "not_ready"
    assert ready.json()["details"] == {"checks": {"repository": "unavailable", "runtime": "ready"}}
    assert "do-not-log" not in ready.text


def test_ready_is_200_when_repository_and_runtime_are_available() -> None:
    application = create_app(InMemoryTaskRepository())
    with TestClient(application) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"repository": "ready", "runtime": "ready"},
    }


def test_ready_is_503_when_embedded_runtime_is_not_accepting() -> None:
    application = create_app(InMemoryTaskRepository())
    with TestClient(application) as client:
        runtime = application.state.task_runtime
        assert runtime is not None
        runtime._accepting = False
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["details"] == {
        "checks": {"repository": "ready", "runtime": "unavailable"}
    }


def test_internal_error_log_uses_the_response_request_id_without_secret() -> None:
    secret = "password=internal-secret"

    class FailingRepository(InMemoryTaskRepository):
        async def get(self, task_id: UUID) -> Task | None:
            raise RuntimeError(secret)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    error_logger = logging.getLogger("teyvatnotes.api.error_handlers")
    original_handlers = list(error_logger.handlers)
    original_level = error_logger.level
    original_propagate = error_logger.propagate
    original_disabled = error_logger.disabled
    error_logger.handlers = [handler]
    error_logger.propagate = False
    error_logger.disabled = False
    error_logger.setLevel(logging.ERROR)

    try:
        application = create_app(FailingRepository())
        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.get(f"/v1/tasks/{uuid4()}")
    finally:
        error_logger.handlers = original_handlers
        error_logger.setLevel(original_level)
        error_logger.propagate = original_propagate
        error_logger.disabled = original_disabled

    record = json.loads(stream.getvalue())
    assert response.status_code == 500
    assert record["event"] == "request_failed"
    assert record["request_id"] == response.headers["X-Request-ID"]
    assert record["exception_type"] == "RuntimeError"
    assert secret not in stream.getvalue()


def test_repository_readiness_probe_has_a_timeout() -> None:
    class HangingProbe:
        cancelled = False

        async def is_ready(self) -> bool:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
            return True

    async def scenario() -> None:
        probe = HangingProbe()
        with pytest.raises(TimeoutError):
            await _repository_ready(probe, timeout_seconds=0.01)
        assert probe.cancelled is True

    asyncio.run(scenario())


def test_metrics_endpoint_reports_success_rate_duration_and_failure_types() -> None:
    class SelectiveExecutor:
        async def execute(self, task: Task) -> JsonValue:
            if task.payload["numbers"] == [0]:
                raise TaskExecutionError("secret upstream detail")
            return {"total": 6}

    application = create_app(InMemoryTaskRepository(), executor=SelectiveExecutor())
    with TestClient(application) as client:
        _create_sum(client, client_request_id="metrics-success")
        failed = client.post(
            "/v1/tasks",
            headers=AUTH_HEADERS,
            json={"task_type": "sum_numbers", "payload": {"numbers": [0]}},
        )
        failed_id = UUID(failed.json()["id"])
        client.get(f"/v1/tasks/{failed_id}/events")
        response = client.get("/metrics")

    body = response.json()
    assert body["started"] == 2
    assert body["completed"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert body["cancelled"] == 0
    assert body["success_rate"] == 0.5
    assert body["duration"]["count"] == 2
    assert body["duration"]["total_seconds"] >= 0
    assert body["duration"]["max_seconds"] >= body["duration"]["average_seconds"]
    assert body["failure_types"] == {"TaskExecutionError": 1}


def test_metrics_registry_does_not_lose_updates_between_threads() -> None:
    registry = MetricsRegistry()
    worker_count = 8
    iterations = 500

    def record_batch() -> None:
        for _ in range(iterations):
            registry.record_started()
            registry.record_completed(
                outcome="failed",
                duration_seconds=0.001,
                failure_type="controlled_failure",
            )

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        list(pool.map(lambda _: record_batch(), range(worker_count)))

    snapshot = registry.snapshot()
    expected = worker_count * iterations
    assert snapshot.started == expected
    assert snapshot.completed == expected
    assert snapshot.failed == expected
    assert snapshot.duration.count == expected
    assert snapshot.failure_types == {"controlled_failure": expected}


def test_worker_metrics_use_final_cancelled_state_when_success_loses_race() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        metrics = MetricsRegistry()

        class CancelThenReturnExecutor:
            async def execute(self, task: Task) -> JsonValue:
                await repository.transition(task.id, TaskStatus.CANCEL_REQUESTED)
                return {"too_late": True}

        task = Task(payload={"numbers": [1]})
        await repository.add(task)
        worker = TaskWorker(
            repository,
            CancelThenReturnExecutor(),
            WorkerSettings(
                worker_id="worker-metrics-cancel",
                lease_duration_seconds=2,
                task_timeout_seconds=1,
            ),
            metrics=metrics,
        )
        assert await worker.run_once() is True

        stored = await repository.get(task.id)
        snapshot = metrics.snapshot()
        assert stored is not None
        assert stored.status is TaskStatus.CANCELLED
        assert snapshot.succeeded == 0
        assert snapshot.failed == 0
        assert snapshot.cancelled == 1
        assert snapshot.failure_types == {}

    asyncio.run(scenario())


def test_embedded_runtime_metrics_use_final_cancelled_state() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        metrics = MetricsRegistry()

        class CancelThenReturnExecutor:
            async def execute(self, task: Task) -> JsonValue:
                await repository.transition(task.id, TaskStatus.CANCEL_REQUESTED)
                return {"too_late": True}

        runtime = TaskRuntime(
            repository,
            CancelThenReturnExecutor(),
            RuntimeSettings(worker_count=1, max_concurrency=1),
            metrics=metrics,
        )
        await runtime.start()
        task = await runtime.create(
            CreateTaskCommand(
                task_type=TaskType.SUM_NUMBERS,
                payload={"numbers": [1]},
            )
        )
        await runtime.wait_until_idle(timeout=1)
        await runtime.stop()

        stored = await repository.get(task.id)
        snapshot = metrics.snapshot()
        assert stored is not None
        assert stored.status is TaskStatus.CANCELLED
        assert snapshot.succeeded == 0
        assert snapshot.failed == 0
        assert snapshot.cancelled == 1

    asyncio.run(scenario())


def test_json_logs_correlate_ids_without_exception_secret() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("test.week6.json")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    task_id = uuid4()
    attempt_id = uuid4()

    with bind_correlation_context(
        request_id="request-123",
        task_id=task_id,
        attempt_id=attempt_id,
        worker_id="worker-1",
    ):
        try:
            raise RuntimeError("api_key=secret-value")
        except RuntimeError:
            logger.exception("任务执行失败", extra={"event": "task_failed"})

    payload = json.loads(stream.getvalue())
    assert payload["request_id"] == "request-123"
    assert payload["task_id"] == str(task_id)
    assert payload["attempt_id"] == str(attempt_id)
    assert payload["worker_id"] == "worker-1"
    assert payload["event"] == "task_failed"
    assert payload["exception_type"] == "RuntimeError"
    assert "secret-value" not in stream.getvalue()


def test_worker_context_can_clear_an_unrelated_inherited_request() -> None:
    task_id = uuid4()
    with bind_correlation_context(request_id="stale-request"):
        with bind_correlation_context(task_id=task_id, inherit=False):
            current = current_correlation_context()
            assert current.request_id is None
            assert current.task_id == task_id
        assert current_correlation_context().request_id == "stale-request"

    assert current_correlation_context().request_id is None


def test_metrics_registry_rejects_invalid_outcome_and_copies_failure_types() -> None:
    registry = MetricsRegistry()
    with pytest.raises(ValueError, match="outcome"):
        registry.record_completed(outcome="unknown", duration_seconds=0)
    with pytest.raises(ValueError, match="duration_seconds"):
        registry.record_completed(outcome="failed", duration_seconds=-1)
    with pytest.raises(ValueError, match="duration_seconds"):
        registry.record_completed(outcome="failed", duration_seconds=float("nan"))
    registry.record_started()
    registry.record_completed(
        outcome="failed",
        duration_seconds=0.25,
        failure_type="timeout",
    )
    first = registry.snapshot()
    first.failure_types["timeout"] = 999
    second = registry.snapshot()

    assert second.failure_types == {"timeout": 1}
    assert second.success_rate == 0
