"""线程安全的内存任务仓储。"""

from copy import deepcopy
from threading import RLock
from uuid import UUID

from agent_runtime_foundation.domain import Task, TaskStatus


class InMemoryTaskRepository:
    """使用字典保存任务，用于开发和 API 测试。"""

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._lock = RLock()

    def add(self, task: Task) -> None:
        with self._lock:
            if task.id in self._tasks:
                raise ValueError(f"任务 {task.id} 已存在")
            self._tasks[task.id] = deepcopy(task)

    def get(self, task_id: UUID) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return deepcopy(task) if task is not None else None

    def list_tasks(
        self,
        *,
        status: TaskStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Task], int]:
        if offset < 0:
            raise ValueError("offset 不能为负数")
        if limit < 1:
            raise ValueError("limit 必须大于 0")

        with self._lock:
            tasks = [
                task for task in self._tasks.values() if status is None or task.status is status
            ]
            tasks.sort(key=lambda task: (task.created_at, task.id.hex))
            total = len(tasks)
            return deepcopy(tasks[offset : offset + limit]), total

    def transition(
        self,
        task_id: UUID,
        target_status: TaskStatus,
    ) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.transition_to(target_status)
            return deepcopy(task)
