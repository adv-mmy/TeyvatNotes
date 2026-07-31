"""应用层输入与输出模型。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from agent_runtime_foundation.domain import JsonValue, Task, TaskType


class TaskAction(StrEnum):
    """对任务发起的应用命令。"""

    CANCEL = "cancel"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class CreateTaskCommand:
    """创建任务用例的输入。"""

    task_type: TaskType
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class TaskPage:
    """分页任务结果。"""

    items: tuple[Task, ...]
    total: int
    offset: int
    limit: int
