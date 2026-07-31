"""任务实体、结果与状态转换规则。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID, uuid4

from agent_runtime_foundation.domain.errors import InvalidTaskTransition

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    """任务在执行生命周期中的状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TaskType(StrEnum):
    """当前明确允许的确定性任务类型。"""

    SUM_NUMBERS = "sum_numbers"
    WORD_COUNT = "word_count"


@dataclass(frozen=True, slots=True)
class TaskResult:
    """任务执行产生的输出或错误。"""

    output: JsonValue = None
    error: str | None = None


@dataclass(slots=True)
class Task:
    """具有受控状态转换行为的任务实体。"""

    id: UUID = field(default_factory=uuid4)
    task_type: TaskType = TaskType.SUM_NUMBERS
    payload: dict[str, JsonValue] = field(default_factory=dict)
    result: TaskResult | None = None
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    _status: TaskStatus = field(default=TaskStatus.QUEUED, init=False, repr=False)

    _ALLOWED_TRANSITIONS: ClassVar[dict[TaskStatus, frozenset[TaskStatus]]] = {
        TaskStatus.QUEUED: frozenset(
            {
                TaskStatus.RUNNING,
                TaskStatus.CANCEL_REQUESTED,
            }
        ),
        TaskStatus.RUNNING: frozenset(
            {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCEL_REQUESTED,
            }
        ),
        TaskStatus.CANCEL_REQUESTED: frozenset({TaskStatus.CANCELLED}),
        TaskStatus.FAILED: frozenset({TaskStatus.QUEUED}),
        TaskStatus.SUCCEEDED: frozenset(),
        TaskStatus.CANCELLED: frozenset(),
    }

    @property
    def status(self) -> TaskStatus:
        """返回只读的当前状态。"""

        return self._status

    def transition_to(self, target_status: TaskStatus) -> None:
        """将任务转换到目标状态；非法转换不会改变任务。"""

        if target_status not in self._ALLOWED_TRANSITIONS[self._status]:
            raise InvalidTaskTransition(self._status.value, target_status.value)

        self._status = target_status
        self.updated_at = _utc_now()
