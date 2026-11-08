"""任务事件模型与查询用例。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from teyvatnotes.application.errors import TaskNotFoundError
from teyvatnotes.application.ports import TaskEventRepository
from teyvatnotes.domain import JsonValue, TaskStatus


class TaskEventType(StrEnum):
    """对客户端公开的任务事件类型。"""

    TASK_CREATED = "task_created"
    STATUS_CHANGED = "status_changed"
    TASK_PROGRESS = "task_progress"


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """可按递增 ID 断点续传的任务事件快照。"""

    id: int
    task_id: UUID
    event_type: TaskEventType
    from_status: TaskStatus | None
    to_status: TaskStatus | None
    payload: dict[str, JsonValue]
    created_at: datetime


class TaskEventService:
    """校验任务存在性并按游标读取事件。"""

    def __init__(self, repository: TaskEventRepository) -> None:
        self._repository = repository

    async def ensure_task_exists(self, task_id: UUID) -> None:
        """使用事件端口在开始 SSE 响应前原子判断任务是否存在。"""

        if await self._repository.list_events(task_id, after_id=0, limit=1) is None:
            raise TaskNotFoundError(task_id)

    async def list_events(
        self,
        task_id: UUID,
        *,
        after_id: int,
        limit: int = 100,
    ) -> tuple[TaskEvent, ...]:
        """返回 ID 严格大于 after_id 的事件。"""

        if after_id < 0:
            raise ValueError("after_id 不能为负数")
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        events = await self._repository.list_events(
            task_id,
            after_id=after_id,
            limit=limit,
        )
        if events is None:
            raise TaskNotFoundError(task_id)
        return events
