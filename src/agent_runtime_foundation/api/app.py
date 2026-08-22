"""FastAPI 应用工厂。"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from typing import cast
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from agent_runtime_foundation.api.error_handlers import register_error_handlers
from agent_runtime_foundation.api.routes.health import router as health_router
from agent_runtime_foundation.api.routes.tasks import router as tasks_router
from agent_runtime_foundation.api.security import (
    ApiKeyAuthenticator,
    SlidingWindowRateLimiter,
)
from agent_runtime_foundation.application import (
    PersistedTaskCommands,
    ReadinessProbe,
    TaskCommandHandler,
    TaskEventRepository,
    TaskRepository,
    TaskService,
)
from agent_runtime_foundation.config import AppSettings, ExecutionMode
from agent_runtime_foundation.database import Database, create_database
from agent_runtime_foundation.observability import MetricsRegistry, bind_correlation_context
from agent_runtime_foundation.repositories import (
    InMemoryTaskRepository,
    PostgresTaskRepository,
)
from agent_runtime_foundation.runtime import (
    DeterministicTaskExecutor,
    RuntimeSettings,
    TaskExecutor,
    TaskRuntime,
)

REQUEST_ID_HEADER = "X-Request-ID"
logger = logging.getLogger(__name__)


def _resolve_request_id() -> str:
    """始终生成服务端请求 ID，避免把客户端可控值直接写入日志。"""

    return str(uuid4())


def _route_template(request: Request) -> str:
    """返回路由模板而非用户提供的原始 URL。"""

    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def create_app(
    repository: TaskRepository | None = None,
    *,
    event_repository: TaskEventRepository | None = None,
    readiness_probe: ReadinessProbe | None = None,
    settings: AppSettings | None = None,
    executor: TaskExecutor | None = None,
    runtime_settings: RuntimeSettings | None = None,
    database_url: str | None = None,
    execution_mode: ExecutionMode | None = None,
    metrics: MetricsRegistry | None = None,
) -> FastAPI:
    """创建可注入仓储、执行器和运行时配置的 FastAPI 应用。"""

    if repository is not None and database_url is not None:
        raise ValueError("repository 和 database_url 不能同时传入")
    if repository is None and (event_repository is not None or readiness_probe is not None):
        raise ValueError("独立 event_repository/readiness_probe 必须与 repository 一起传入")

    resolved_settings = settings or AppSettings.from_env()
    database: Database | None = None
    resolved_database_url = database_url
    if resolved_database_url is None and resolved_settings.database_url is not None:
        resolved_database_url = resolved_settings.database_url.get_secret_value()
    if repository is not None:
        task_repository = repository
    elif resolved_database_url is not None:
        database = create_database(resolved_database_url)
        task_repository = PostgresTaskRepository(database.session_factory)
    else:
        task_repository = InMemoryTaskRepository()
    task_event_repository = event_repository or cast(
        TaskEventRepository,
        _require_capability(
            task_repository,
            method_name="list_events",
            capability="TaskEventRepository",
        ),
    )
    resolved_readiness_probe = readiness_probe or cast(
        ReadinessProbe,
        _require_capability(
            task_repository,
            method_name="is_ready",
            capability="ReadinessProbe",
        ),
    )
    metrics_registry = metrics or MetricsRegistry()
    resolved_execution_mode = execution_mode or resolved_settings.execution_mode
    resolved_runtime_settings = runtime_settings or resolved_settings.runtime
    task_runtime: TaskRuntime | None
    task_commands: TaskCommandHandler
    if resolved_execution_mode == "embedded":
        task_executor = executor if executor is not None else DeterministicTaskExecutor()
        task_runtime = TaskRuntime(
            repository=task_repository,
            executor=task_executor,
            settings=resolved_runtime_settings,
            metrics=metrics_registry,
        )
        task_commands = task_runtime
    elif resolved_execution_mode == "api_only":
        task_runtime = None
        task_commands = PersistedTaskCommands(TaskService(task_repository))
    else:
        raise ValueError(f"不支持的 execution_mode：{resolved_execution_mode}")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if task_runtime is not None:
            await task_runtime.start()
        try:
            yield
        finally:
            try:
                if task_runtime is not None:
                    await task_runtime.stop()
            finally:
                if database is not None:
                    await database.dispose()

    application = FastAPI(
        title="Agent Runtime Foundation",
        version="0.8.0",
        lifespan=lifespan,
    )
    application.state.task_repository = task_repository
    application.state.task_event_repository = task_event_repository
    application.state.readiness_probe = resolved_readiness_probe
    application.state.task_runtime = task_runtime
    application.state.task_commands = task_commands
    application.state.execution_mode = resolved_execution_mode
    application.state.database = database
    application.state.metrics = metrics_registry
    application.state.api_key_authenticator = ApiKeyAuthenticator(resolved_settings.api_key)
    application.state.rate_limiter = SlidingWindowRateLimiter(resolved_settings.rate_limit)

    @application.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = _resolve_request_id()
        request.state.request_id = request_id
        started_at = perf_counter()
        with bind_correlation_context(request_id=request_id, inherit=False):
            logger.info(
                "HTTP 请求开始",
                extra={
                    "event": "http_request_started",
                    "http_method": request.method,
                },
            )
            response = await call_next(request)
            duration_ms = round((perf_counter() - started_at) * 1000, 3)
            logger.info(
                "HTTP 请求结束",
                extra={
                    "event": "http_request_completed",
                    "http_method": request.method,
                    "http_path": _route_template(request),
                    "http_status": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            return response

    register_error_handlers(application)
    application.include_router(health_router)
    application.include_router(tasks_router, prefix="/v1")
    return application


def _require_capability(
    value: object,
    *,
    method_name: str,
    capability: str,
) -> object:
    """在组合根尽早拒绝缺少周边能力的依赖，避免请求期间才出现 500。"""

    if not callable(getattr(value, method_name, None)):
        raise TypeError(f"repository 未实现 {capability}.{method_name}()")
    return value
