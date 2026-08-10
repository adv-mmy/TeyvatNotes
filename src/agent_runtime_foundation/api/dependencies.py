"""FastAPI 依赖注入函数。"""

from typing import Annotated, cast

from fastapi import Depends, Request

from agent_runtime_foundation.application import (
    TaskCommandHandler,
    TaskRepository,
    TaskService,
)
from agent_runtime_foundation.runtime import TaskRuntime


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


def get_task_runtime(request: Request) -> TaskRuntime:
    """从应用状态中取得共享的异步任务运行时。"""

    runtime = getattr(request.app.state, "task_runtime", None)
    if runtime is None:
        raise RuntimeError("应用未配置 TaskRuntime")
    return cast(TaskRuntime, runtime)


def get_task_commands(request: Request) -> TaskCommandHandler:
    """取得当前应用的任务命令处理器。"""

    commands = getattr(request.app.state, "task_commands", None)
    if commands is None:
        raise RuntimeError("应用未配置 TaskCommandHandler")
    return cast(TaskCommandHandler, commands)
