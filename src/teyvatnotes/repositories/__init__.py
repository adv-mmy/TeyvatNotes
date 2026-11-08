"""任务仓储协议与内存实现。"""

from teyvatnotes.repositories.base import TaskRepository
from teyvatnotes.repositories.memory import InMemoryTaskRepository
from teyvatnotes.repositories.postgres import (
    PostgresTaskRepository,
    PostgresWorkerRepository,
)

__all__ = [
    "InMemoryTaskRepository",
    "PostgresTaskRepository",
    "PostgresWorkerRepository",
    "TaskRepository",
]
