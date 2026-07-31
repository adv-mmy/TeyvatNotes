"""FastAPI 应用工厂。"""

from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from agent_runtime_foundation.api.error_handlers import register_error_handlers
from agent_runtime_foundation.api.routes.tasks import router as tasks_router
from agent_runtime_foundation.application import TaskRepository
from agent_runtime_foundation.repositories import InMemoryTaskRepository

REQUEST_ID_HEADER = "X-Request-ID"


def create_app(repository: TaskRepository | None = None) -> FastAPI:
    """创建可注入仓储实现的 FastAPI 应用。"""

    application = FastAPI(
        title="Agent Runtime Foundation",
        version="0.2.0",
    )
    application.state.task_repository = (
        repository if repository is not None else InMemoryTaskRepository()
    )

    @application.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    register_error_handlers(application)
    application.include_router(tasks_router, prefix="/v1")
    return application


app = create_app()
