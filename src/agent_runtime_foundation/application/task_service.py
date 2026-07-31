"""创建、查询、列表、取消和重试任务的应用服务。"""

from uuid import UUID

from agent_runtime_foundation.application.errors import (
    TaskActionConflictError,
    TaskNotFoundError,
)
from agent_runtime_foundation.application.models import (
    CreateTaskCommand,
    TaskAction,
    TaskPage,
)
from agent_runtime_foundation.application.ports import TaskRepository
from agent_runtime_foundation.domain import InvalidTaskTransition, Task, TaskStatus


class TaskService:
    """编排任务用例，不感知 HTTP 或具体存储实现。"""

    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository

    def create(self, command: CreateTaskCommand) -> Task:
        task = Task(
            task_type=command.task_type,
            payload=dict(command.payload),
        )
        self._repository.add(task)
        return task

    def get(self, task_id: UUID) -> Task:
        task = self._repository.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def list_tasks(
        self,
        *,
        status: TaskStatus | None,
        offset: int,
        limit: int,
    ) -> TaskPage:
        tasks, total = self._repository.list_tasks(
            status=status,
            offset=offset,
            limit=limit,
        )
        return TaskPage(
            items=tuple(tasks),
            total=total,
            offset=offset,
            limit=limit,
        )

    def cancel(self, task_id: UUID) -> Task:
        return self._transition(
            task_id=task_id,
            target_status=TaskStatus.CANCEL_REQUESTED,
            action=TaskAction.CANCEL,
        )

    def retry(self, task_id: UUID) -> Task:
        return self._transition(
            task_id=task_id,
            target_status=TaskStatus.QUEUED,
            action=TaskAction.RETRY,
        )

    def _transition(
        self,
        *,
        task_id: UUID,
        target_status: TaskStatus,
        action: TaskAction,
    ) -> Task:
        try:
            task = self._repository.transition(task_id, target_status)
        except InvalidTaskTransition as exc:
            raise TaskActionConflictError(
                task_id=task_id,
                from_status=TaskStatus(exc.current_status),
                action=action,
            ) from exc

        if task is None:
            raise TaskNotFoundError(task_id)
        return task
