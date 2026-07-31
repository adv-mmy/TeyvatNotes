"""应用用例异常。"""

from uuid import UUID

from agent_runtime_foundation.application.models import TaskAction
from agent_runtime_foundation.domain import TaskStatus


class TaskNotFoundError(LookupError):
    """请求的任务不存在。"""

    def __init__(self, task_id: UUID) -> None:
        self.task_id = task_id
        super().__init__(f"任务 {task_id} 不存在")


class TaskActionConflictError(RuntimeError):
    """任务当前状态不允许执行指定动作。"""

    def __init__(
        self,
        *,
        task_id: UUID,
        from_status: TaskStatus,
        action: TaskAction,
    ) -> None:
        self.task_id = task_id
        self.from_status = from_status
        self.action = action
        super().__init__(f"任务 {task_id} 处于 {from_status.value!r} 时不能执行 {action.value!r}")
