"""任务领域模型。"""

from agent_runtime_foundation.domain.errors import InvalidTaskTransition
from agent_runtime_foundation.domain.task import Task, TaskResult, TaskStatus

__all__ = [
    "InvalidTaskTransition",
    "Task",
    "TaskResult",
    "TaskStatus",
]
