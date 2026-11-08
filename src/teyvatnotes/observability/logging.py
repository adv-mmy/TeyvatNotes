"""不记录请求体和异常原文的 JSON 日志。"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from teyvatnotes.observability.context import current_correlation_context

_SAFE_EXTRA_FIELDS = (
    "event",
    "http_method",
    "http_path",
    "http_status",
    "duration_ms",
    "failure_type",
    "task_status",
    "exception_type",
)


class JsonLogFormatter(logging.Formatter):
    """输出单行 JSON，并严格限制可附加字段。"""

    def format(self, record: logging.LogRecord) -> str:
        context = current_correlation_context()
        message = (
            "ASGI 服务器事件"
            if record.name == "uvicorn.error" or record.name.startswith("uvicorn.error.")
            else record.getMessage()
        )
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        for name, value in (
            ("request_id", context.request_id),
            ("task_id", context.task_id),
            ("attempt_id", context.attempt_id),
            ("worker_id", context.worker_id),
        ):
            if value is not None:
                payload[name] = str(value)
        for name in _SAFE_EXTRA_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if (
            "exception_type" not in payload
            and record.exc_info is not None
            and record.exc_info[0] is not None
        ):
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    """安装安全 JSON 日志，并关闭会记录原始 URL 的 Uvicorn access log。"""

    project_logger = logging.getLogger("teyvatnotes")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    handler._agent_runtime_json = True  # type: ignore[attr-defined]

    _replace_with_safe_handler(project_logger, handler=handler, level=level)
    _replace_with_safe_handler(
        logging.getLogger("uvicorn.error"),
        handler=handler,
        level=level,
    )

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True


def _replace_with_safe_handler(
    target: logging.Logger,
    *,
    handler: logging.Handler,
    level: int,
) -> None:
    """移除可能展开异常原文的 handler，只保留安全 JSON 输出。"""

    safe_handler = next(
        (item for item in target.handlers if getattr(item, "_agent_runtime_json", False)),
        handler,
    )
    target.handlers.clear()
    target.addHandler(safe_handler)
    target.disabled = False
    target.setLevel(level)
    target.propagate = False
