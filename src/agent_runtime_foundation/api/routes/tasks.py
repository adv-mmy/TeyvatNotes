"""任务 API 路由。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from agent_runtime_foundation.api.dependencies import (
    get_task_commands,
    get_task_event_service,
    get_task_service,
)
from agent_runtime_foundation.api.schemas import (
    ErrorResponse,
    TaskCreateRequest,
    TaskListResponse,
    TaskResponse,
    to_create_command,
)
from agent_runtime_foundation.api.sse import stream_task_events
from agent_runtime_foundation.application import (
    TaskCommandHandler,
    TaskEventService,
    TaskService,
)
from agent_runtime_foundation.domain import TaskStatus

router = APIRouter(prefix="/tasks", tags=["任务"])
ServiceDependency = Annotated[TaskService, Depends(get_task_service)]
CommandsDependency = Annotated[TaskCommandHandler, Depends(get_task_commands)]
EventServiceDependency = Annotated[TaskEventService, Depends(get_task_event_service)]


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建任务",
    responses={
        201: {
            "description": "任务已创建",
            "headers": {
                "Location": {
                    "description": "新任务的查询路径",
                    "schema": {"type": "string"},
                }
            },
        },
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def create_task(
    payload: TaskCreateRequest,
    response: Response,
    commands: CommandsDependency,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ] = None,
) -> TaskResponse:
    task = await commands.create(to_create_command(payload, idempotency_key=idempotency_key))
    response.headers["Location"] = f"/v1/tasks/{task.id}"
    return TaskResponse.from_task(task)


@router.get(
    "",
    response_model=TaskListResponse,
    summary="列出任务",
    responses={
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def list_tasks(
    service: ServiceDependency,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TaskListResponse:
    page = await service.list_tasks(
        status=task_status,
        offset=offset,
        limit=limit,
    )
    return TaskListResponse(
        items=[TaskResponse.from_task(task) for task in page.items],
        total=page.total,
        offset=page.offset,
        limit=page.limit,
    )


@router.get(
    "/{task_id}/events",
    summary="订阅任务事件",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "SSE 事件流",
            "content": {"text/event-stream": {}},
        },
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def stream_events(
    task_id: UUID,
    request: Request,
    service: ServiceDependency,
    event_service: EventServiceDependency,
    last_event_id: Annotated[
        int | None,
        Header(alias="Last-Event-ID", ge=0),
    ] = None,
) -> StreamingResponse:
    """从 Last-Event-ID 之后推送事件，不重复已确认事件。"""

    await event_service.ensure_task_exists(task_id)
    return StreamingResponse(
        stream_task_events(
            request,
            task_id=task_id,
            after_id=last_event_id or 0,
            event_service=event_service,
            task_service=service,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    summary="查询任务",
    responses={
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_task(task_id: UUID, service: ServiceDependency) -> TaskResponse:
    return TaskResponse.from_task(await service.get(task_id))


@router.post(
    "/{task_id}/cancel",
    response_model=TaskResponse,
    summary="请求取消任务",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def cancel_task(task_id: UUID, commands: CommandsDependency) -> TaskResponse:
    return TaskResponse.from_task(await commands.cancel(task_id))


@router.post(
    "/{task_id}/retry",
    response_model=TaskResponse,
    summary="重试失败任务",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def retry_task(task_id: UUID, commands: CommandsDependency) -> TaskResponse:
    return TaskResponse.from_task(await commands.retry(task_id))
