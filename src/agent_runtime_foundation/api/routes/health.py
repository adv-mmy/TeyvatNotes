"""存活、就绪和进程内指标接口。"""

import asyncio
import logging
from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_runtime_foundation.api.schemas import (
    ErrorResponse,
    HealthResponse,
    MetricsResponse,
)
from agent_runtime_foundation.application import ReadinessProbe
from agent_runtime_foundation.domain import JsonValue
from agent_runtime_foundation.observability import MetricsRegistry
from agent_runtime_foundation.runtime import TaskRuntime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["可观测性"])
_READINESS_TIMEOUT_SECONDS = 1.0


@router.get("/health/live", response_model=HealthResponse, summary="存活检查")
async def live() -> HealthResponse:
    """进程能处理 HTTP 时即为存活。"""

    return HealthResponse(status="ok", checks={"process": "ready"})


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    responses={503: {"model": ErrorResponse}},
    summary="就绪检查",
)
async def ready(request: Request) -> HealthResponse | JSONResponse:
    """存储可用，且内嵌 Runtime 可接收任务时返回 200。"""

    readiness_probe: ReadinessProbe = request.app.state.readiness_probe
    task_runtime = request.app.state.task_runtime
    checks: dict[str, str] = {}
    try:
        repository_ready = await _repository_ready(readiness_probe)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "存储就绪检查失败",
            extra={
                "event": "readiness_failed",
                "failure_type": "repository",
                "exception_type": type(exc).__name__,
            },
        )
        repository_ready = False
    checks["repository"] = "ready" if repository_ready else "unavailable"

    runtime_ready = _runtime_ready(task_runtime)
    checks["runtime"] = "ready" if runtime_ready else "unavailable"
    if repository_ready and runtime_ready:
        return HealthResponse(status="ok", checks=checks)

    request_id = request.state.request_id
    details: JsonValue = {"checks": dict(checks)}
    body = ErrorResponse(
        code="not_ready",
        message="服务尚未就绪",
        details=details,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        content=body.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


@router.get("/metrics", response_model=MetricsResponse, summary="进程内任务指标")
async def metrics(request: Request) -> MetricsResponse:
    """返回当前进程的 attempt 成功率、耗时和失败类型。"""

    registry: MetricsRegistry = request.app.state.metrics
    return MetricsResponse.from_snapshot(registry.snapshot())


async def _repository_ready(
    probe: ReadinessProbe,
    *,
    timeout_seconds: float = _READINESS_TIMEOUT_SECONDS,
) -> bool:
    async with asyncio.timeout(timeout_seconds):
        return await probe.is_ready()


def _runtime_ready(runtime: TaskRuntime | None) -> bool:
    return runtime is None or runtime.snapshot.accepting
