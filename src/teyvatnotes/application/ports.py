"""应用层依赖的外部端口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from teyvatnotes.application.models import CreateTaskCommand, TaskCreation
from teyvatnotes.domain import Task, TaskResult, TaskStatus

if TYPE_CHECKING:
    from teyvatnotes.application.events import TaskEvent


class TaskRepository(Protocol):
    """任务持久化端口，由基础设施层提供实现。"""

    async def add(self, task: Task) -> TaskCreation:
        """新增任务；幂等键已存在时返回首次创建的任务。"""

    async def get(self, task_id: UUID) -> Task | None:
        """按 ID 查询任务。"""

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Task], int]:
        """返回当前页任务及过滤后的总数。"""

    async def transition(
        self,
        task_id: UUID,
        target_status: TaskStatus,
        *,
        result: TaskResult | None = None,
        attempt_id: UUID | None = None,
    ) -> Task | None:
        """原子地转换已存在任务的状态并替换执行结果。"""


class TaskEventRepository(Protocol):
    """任务事件游标查询端口。"""

    async def list_events(
        self,
        task_id: UUID,
        *,
        after_id: int,
        limit: int,
    ) -> tuple[TaskEvent, ...] | None:
        """按递增 ID 返回事件；任务不存在时返回 None。"""


class ReadinessProbe(Protocol):
    """存储依赖的就绪检查端口。"""

    async def is_ready(self) -> bool:
        """检查存储依赖是否可接受请求。"""


class TaskCommandHandler(Protocol):
    """API 可调用的创建、取消和手动重试命令端口。"""

    async def create(self, command: CreateTaskCommand) -> Task:
        """接收一个新任务。"""

    async def cancel(self, task_id: UUID) -> Task:
        """请求取消任务。"""

    async def retry(self, task_id: UUID) -> Task:
        """手动重试失败任务。"""
