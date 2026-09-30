"""只提供健康与指标读取的 Worker 监控应用，不接受业务任务。"""

import asyncio
import logging
from collections.abc import Callable
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import Lifespan

from agent_runtime_foundation.api.schemas import ErrorResponse, HealthResponse, MetricsResponse
from agent_runtime_foundation.application import ReadinessProbe
from agent_runtime_foundation.observability import MetricsRegistry

logger = logging.getLogger(__name__)


def create_worker_monitoring_app(
    *,
    metrics: MetricsRegistry,
    readiness_probe: ReadinessProbe,
    worker_running: Callable[[], bool],
    readiness_timeout_seconds: float = 1.0,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """共享 Worker 的真实指标，并有界探测执行循环与数据库表。"""

    application = FastAPI(title="Worker 监控", lifespan=lifespan, docs_url=None, redoc_url=None)

    @application.get("/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="ok", checks={"process": "ready"})

    @application.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def ready() -> HealthResponse | JSONResponse:
        repository_ready = False
        try:
            async with asyncio.timeout(readiness_timeout_seconds):
                repository_ready = await readiness_probe.is_ready()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Worker 存储就绪检查失败",
                extra={"event": "readiness_failed", "exception_type": type(exc).__name__},
            )
        running = worker_running()
        checks = {
            "repository": "ready" if repository_ready else "unavailable",
            "worker": "ready" if running else "unavailable",
        }
        if repository_ready and running:
            return HealthResponse(status="ok", checks=checks)
        request_id = str(uuid4())
        body = ErrorResponse(
            code="not_ready",
            message="Worker 尚未就绪",
            details={"checks": dict(checks)},
            request_id=request_id,
        )
        return JSONResponse(
            status_code=503,
            content=body.model_dump(mode="json"),
            headers={"X-Request-ID": request_id},
        )

    @application.get("/metrics", response_model=MetricsResponse)
    async def snapshot() -> MetricsResponse:
        return MetricsResponse.from_snapshot(metrics.snapshot())

    return application
