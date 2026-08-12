"""API 统一异常处理。"""

import logging
from http import HTTPStatus
from typing import cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_runtime_foundation.api.errors import ErrorCode
from agent_runtime_foundation.api.schemas import ErrorResponse
from agent_runtime_foundation.application import (
    TaskAction,
    TaskActionConflictError,
    TaskNotFoundError,
)
from agent_runtime_foundation.domain import JsonValue
from agent_runtime_foundation.observability import bind_correlation_context
from agent_runtime_foundation.runtime import RuntimeNotAcceptingError

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str):
        return request_id
    request_id = str(uuid4())
    request.state.request_id = request_id
    return request_id


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: JsonValue = None,
) -> JSONResponse:
    request_id = _request_id(request)
    body = ErrorResponse(
        code=code,
        message=message,
        details={} if details is None else details,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


async def task_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    error = cast(TaskNotFoundError, exc)
    return _error_response(
        request,
        status_code=HTTPStatus.NOT_FOUND,
        code=ErrorCode.TASK_NOT_FOUND,
        message="任务不存在",
        details={"task_id": str(error.task_id)},
    )


async def task_action_conflict_handler(request: Request, exc: Exception) -> JSONResponse:
    error = cast(TaskActionConflictError, exc)
    message = (
        "当前状态不允许取消任务" if error.action is TaskAction.CANCEL else "当前状态不允许重试任务"
    )
    return _error_response(
        request,
        status_code=HTTPStatus.CONFLICT,
        code=ErrorCode.INVALID_TASK_TRANSITION,
        message=message,
        details={
            "task_id": str(error.task_id),
            "from_status": error.from_status.value,
            "action": error.action.value,
        },
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    error = cast(RequestValidationError, exc)
    details: list[JsonValue] = []
    for issue in error.errors():
        location: list[JsonValue] = [
            part if isinstance(part, (str, int)) else str(part) for part in issue["loc"]
        ]
        detail: dict[str, JsonValue] = {
            "location": location,
            "message": issue["msg"],
            "type": issue["type"],
        }
        details.append(detail)
    return _error_response(
        request,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code=ErrorCode.VALIDATION_ERROR,
        message="请求参数校验失败",
        details={"errors": details},
    )


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    error = cast(StarletteHTTPException, exc)
    message = "请求的资源不存在" if error.status_code == 404 else "HTTP 请求失败"
    return _error_response(
        request,
        status_code=error.status_code,
        code=ErrorCode.HTTP_ERROR,
        message=message,
        details={"status_code": error.status_code},
    )


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = _request_id(request)
    with bind_correlation_context(request_id=request_id, inherit=False):
        logger.error(
            "未处理异常",
            extra={"event": "request_failed", "exception_type": type(exc).__name__},
        )
    return _error_response(
        request,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code=ErrorCode.INTERNAL_ERROR,
        message="服务器内部错误",
    )


async def runtime_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    cast(RuntimeNotAcceptingError, exc)
    return _error_response(
        request,
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        code=ErrorCode.RUNTIME_UNAVAILABLE,
        message="任务运行时正在启动或停止，请稍后重试",
    )


def register_error_handlers(app: FastAPI) -> None:
    """注册应用的统一错误处理器。"""

    app.add_exception_handler(TaskNotFoundError, task_not_found_handler)
    app.add_exception_handler(TaskActionConflictError, task_action_conflict_handler)
    app.add_exception_handler(RuntimeNotAcceptingError, runtime_unavailable_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)
