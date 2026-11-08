"""独立 Worker 的实际指标、故障探测与监控配置测试。"""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

import worker_main
from teyvatnotes.api.worker_monitoring import create_worker_monitoring_app
from teyvatnotes.config import ConfigurationError, WorkerProcessSettings
from teyvatnotes.database import create_database
from teyvatnotes.domain import Task
from teyvatnotes.observability import MetricsRegistry
from teyvatnotes.repositories import InMemoryTaskRepository
from teyvatnotes.runtime import DeterministicTaskExecutor
from teyvatnotes.worker import TaskWorker, WorkerSettings


class Probe:
    """分别模拟可用、失联与阻塞的存储，错误内容不能进入响应。"""

    def __init__(self, mode: str = "ready") -> None:
        self.mode = mode

    async def is_ready(self) -> bool:
        if self.mode == "error":
            raise RuntimeError("private-database-password")
        if self.mode == "blocked":
            await asyncio.Event().wait()
        return self.mode == "ready"


def test_monitoring_reports_worker_execution_in_shared_registry() -> None:
    repository = InMemoryTaskRepository()
    metrics = MetricsRegistry()
    worker = TaskWorker(
        repository,
        DeterministicTaskExecutor(),
        WorkerSettings(worker_id="monitored-worker"),
        metrics=metrics,
    )

    async def execute() -> None:
        await repository.add(Task(payload={"numbers": [1, 2]}))
        assert await worker.run_once()

    asyncio.run(execute())
    application = create_worker_monitoring_app(
        metrics=metrics, readiness_probe=Probe(), worker_running=lambda: True
    )
    with TestClient(application) as client:
        snapshot = client.get("/metrics").json()
        assert client.get("/health/ready").status_code == 200
        assert client.post("/v1/tasks", json={}).status_code == 404
    assert snapshot["started"] == snapshot["completed"] == snapshot["succeeded"] == 1
    assert snapshot["success_rate"] == 1
    assert snapshot["duration"]["count"] == 1


@pytest.mark.parametrize("mode", ["error", "blocked", "unavailable"])
def test_monitoring_readiness_is_bounded_and_hides_errors(mode: str) -> None:
    application = create_worker_monitoring_app(
        metrics=MetricsRegistry(),
        readiness_probe=Probe(mode),
        worker_running=lambda: True,
        readiness_timeout_seconds=0.01,
    )
    with TestClient(application) as client:
        response = client.get("/health/ready")
        assert client.get("/health/live").status_code == 200
    assert response.status_code == 503
    assert response.json()["code"] == "not_ready"
    assert response.json()["details"]["checks"]["repository"] == "unavailable"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert "private-database-password" not in response.text


def test_monitoring_rejects_stopped_worker_even_when_database_is_ready() -> None:
    application = create_worker_monitoring_app(
        metrics=MetricsRegistry(), readiness_probe=Probe(), worker_running=lambda: False
    )
    with TestClient(application) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["details"]["checks"]["worker"] == "unavailable"


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_worker_monitoring_port_is_validated(port: str) -> None:
    with pytest.raises(ConfigurationError, match="AGENT_RUNTIME_WORKER_MONITOR_PORT"):
        WorkerProcessSettings.from_env(
            {
                "DATABASE_URL": "postgresql+asyncpg://example/runtime",
                "AGENT_RUNTIME_WORKER_MONITOR_PORT": port,
            }
        )


def test_worker_monitoring_defaults_to_loopback_and_accepts_custom_port() -> None:
    settings = WorkerProcessSettings.from_env(
        {"DATABASE_URL": "postgresql+asyncpg://example/runtime"}
    )
    assert settings.monitor_host == "127.0.0.1"
    assert settings.monitor_port == 9101
    with pytest.raises(ConfigurationError, match="AGENT_RUNTIME_WORKER_MONITOR_HOST"):
        WorkerProcessSettings.from_env(
            {
                "DATABASE_URL": "postgresql+asyncpg://example/runtime",
                "AGENT_RUNTIME_WORKER_MONITOR_HOST": "invalid",
            }
        )


@pytest.mark.parametrize("mode", ["idle", "busy", "fatal"])
def test_worker_entrypoint_supervises_loop_and_disposes_database(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """不绑定真实端口，直接进入正式 lifespan 验证启停与循环失败。"""

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        stop = asyncio.Event()
        disposed = False
        settings = WorkerProcessSettings.from_env(
            {"DATABASE_URL": "postgresql+asyncpg://example/runtime"}
        )
        database = create_database(settings.database_url.get_secret_value())
        original_dispose = type(database).dispose

        async def dispose(instance: Any) -> None:
            nonlocal disposed
            disposed = True
            await original_dispose(instance)

        class FakeWorker:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def run_forever(self, stop_event: asyncio.Event) -> None:
                entered.set()
                if mode == "fatal":
                    raise RuntimeError("worker loop failed")
                if mode == "busy":
                    await release.wait()
                await stop_event.wait()

        class FakeServer:
            def __init__(self, config: Any) -> None:
                self.config = config
                self.should_exit = False

            async def serve(self) -> None:
                async with self.config.app.router.lifespan_context(self.config.app):
                    while not self.should_exit:
                        await asyncio.sleep(0)

        monkeypatch.setattr(worker_main, "TaskWorker", FakeWorker)
        monkeypatch.setattr(worker_main.uvicorn, "Server", FakeServer)
        monkeypatch.setattr(type(database), "dispose", dispose)
        running = asyncio.create_task(
            worker_main.run_worker(settings, stop_event=stop, database=database)
        )
        await entered.wait()
        if mode == "fatal":
            with pytest.raises(RuntimeError, match="worker loop failed"):
                await running
        else:
            stop.set()
            if mode == "busy":
                await asyncio.sleep(0)
                assert not running.done()
                release.set()
            await running
        assert disposed

    asyncio.run(scenario())
