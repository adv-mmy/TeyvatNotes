"""线程安全的内存任务仓储。"""

from copy import deepcopy
from threading import RLock
from uuid import UUID

from agent_runtime_foundation.application import TaskCreation
from agent_runtime_foundation.domain import Task, TaskResult, TaskStatus


class InMemoryTaskRepository:
    """使用字典保存任务，用于开发和 API 测试。"""

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._idempotency_keys: dict[str, UUID] = {}
        self._lock = RLock()

    async def add(self, task: Task) -> TaskCreation:
        with self._lock:
            if task.id in self._tasks:
                raise ValueError(f"任务 {task.id} 已存在")
            if task.idempotency_key is not None:
                existing_id = self._idempotency_keys.get(task.idempotency_key)
                if existing_id is not None:
                    return TaskCreation(task=deepcopy(self._tasks[existing_id]), created=False)
            self._tasks[task.id] = deepcopy(task)
            if task.idempotency_key is not None:
                self._idempotency_keys[task.idempotency_key] = task.id
            return TaskCreation(task=deepcopy(task), created=True)

    async def get(self, task_id: UUID) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return deepcopy(task) if task is not None else None

    async def list_tasks(
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

    async def transition(
        self,
        task_id: UUID,
        target_status: TaskStatus,
        *,
        result: TaskResult | None = None,
        attempt_id: UUID | None = None,
    ) -> Task | None:
        del attempt_id
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.transition_to(target_status, result=deepcopy(result))
            return deepcopy(task)
