"""请求、任务和执行尝试的日志关联上下文。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """一条请求到 Worker attempt 的关联标识。"""

    request_id: str | None = None
    task_id: UUID | None = None
    attempt_id: UUID | None = None
    worker_id: str | None = None


_CORRELATION_CONTEXT: ContextVar[CorrelationContext | None] = ContextVar(
    "agent_runtime_correlation_context",
    default=None,
)


def current_correlation_context() -> CorrelationContext:
    """返回当前协程的不可变关联上下文。"""

    return _CORRELATION_CONTEXT.get() or CorrelationContext()


def current_request_id() -> str | None:
    """返回当前请求 ID；非 HTTP 调用时可为 None。"""

    return current_correlation_context().request_id


@contextmanager
def bind_correlation_context(
    *,
    request_id: str | None = None,
    task_id: UUID | None = None,
    attempt_id: UUID | None = None,
    worker_id: str | None = None,
    inherit: bool = True,
) -> Iterator[CorrelationContext]:
    """在 with 作用域内绑定关联 ID，退出时自动恢复。"""

    previous = current_correlation_context() if inherit else CorrelationContext()
    context = CorrelationContext(
        request_id=request_id if request_id is not None else previous.request_id,
        task_id=task_id if task_id is not None else previous.task_id,
        attempt_id=attempt_id if attempt_id is not None else previous.attempt_id,
        worker_id=worker_id if worker_id is not None else previous.worker_id,
    )
    token = _CORRELATION_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CORRELATION_CONTEXT.reset(token)
