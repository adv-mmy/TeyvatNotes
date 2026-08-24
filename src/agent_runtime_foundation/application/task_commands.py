"""API 进程只持久化命令时使用的应用服务。"""

from uuid import UUID

from agent_runtime_foundation.application.models import CreateTaskCommand
from agent_runtime_foundation.application.task_service import TaskService
from agent_runtime_foundation.domain import Task


class PersistedTaskCommands:
    """不启动内嵌 Worker，只把 API 命令保存到 Repository。"""

    def __init__(self, service: TaskService) -> None:
        self._service = service

    async def create(self, command: CreateTaskCommand) -> Task:
        creation = await self._service.create(command)
        return creation.task

    async def cancel(self, task_id: UUID) -> Task:
        return await self._service.cancel(task_id)

    async def retry(self, task_id: UUID) -> Task:
        return await self._service.retry(task_id)
