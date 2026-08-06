"""应用层依赖的外部端口。"""

from typing import Protocol
from uuid import UUID

from agent_runtime_foundation.domain import Task, TaskResult, TaskStatus


class TaskRepository(Protocol):
    """任务持久化端口，由基础设施层提供实现。"""

    def add(self, task: Task) -> None:
        """新增任务。"""

    def get(self, task_id: UUID) -> Task | None:
        """按 ID 查询任务。"""

    def list_tasks(
        self,
        *,
        status: TaskStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Task], int]:
        """返回当前页任务及过滤后的总数。"""

    def transition(
        self,
        task_id: UUID,
        target_status: TaskStatus,
        *,
        result: TaskResult | None = None,
    ) -> Task | None:
        """原子地转换已存在任务的状态并替换执行结果。"""
