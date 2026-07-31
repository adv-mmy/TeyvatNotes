"""FastAPI 依赖注入函数。"""

from typing import Annotated, cast

from fastapi import Depends, Request

from agent_runtime_foundation.application import TaskRepository, TaskService


def get_task_repository(request: Request) -> TaskRepository:
    """从应用状态中取得任务仓储。"""

    repository = getattr(request.app.state, "task_repository", None)
    if repository is None:
        raise RuntimeError("应用未配置 TaskRepository")
    return cast(TaskRepository, repository)


RepositoryDependency = Annotated[TaskRepository, Depends(get_task_repository)]


def get_task_service(repository: RepositoryDependency) -> TaskService:
    """为当前请求构造使用共享 Repository 的应用服务。"""

    return TaskService(repository)
