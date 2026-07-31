"""API 公开错误代码。"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """可在 HTTP 契约中稳定使用的错误代码。"""

    TASK_NOT_FOUND = "task_not_found"
    INVALID_TASK_TRANSITION = "invalid_task_transition"
    VALIDATION_ERROR = "validation_error"
    INTERNAL_ERROR = "internal_error"
    HTTP_ERROR = "http_error"
