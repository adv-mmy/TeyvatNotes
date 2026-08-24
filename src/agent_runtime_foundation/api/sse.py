"""SSE 编码、断点游标和终态收敛。"""

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from uuid import UUID

from fastapi import Request

from agent_runtime_foundation.application import TaskEvent, TaskEventService, TaskService
from agent_runtime_foundation.domain import TaskStatus

_TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}


def encode_sse_event(event: TaskEvent) -> str:
    """把一个任务事件编码为单条 SSE 消息。"""

    data = json.dumps(
        {
            "id": event.id,
            "task_id": str(event.task_id),
            "event_type": event.event_type.value,
            "from_status": event.from_status.value if event.from_status is not None else None,
            "to_status": event.to_status.value if event.to_status is not None else None,
            "payload": event.payload,
            "created_at": event.created_at.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.id}\nevent: {event.event_type.value}\ndata: {data}\n\n"


async def stream_task_events(
    request: Request,
    *,
    task_id: UUID,
    after_id: int,
    event_service: TaskEventService,
    task_service: TaskService,
    poll_interval_seconds: float = 0.05,
    heartbeat_seconds: float = 15.0,
    batch_size: int = 100,
) -> AsyncIterator[str]:
    """从 after_id 之后开始推送，无事件时发心跳，终态后关闭。"""

    cursor = after_id
    last_heartbeat = monotonic()
    while True:
        events = await event_service.list_events(
            task_id,
            after_id=cursor,
            limit=batch_size,
        )
        for event in events:
            cursor = event.id
            yield encode_sse_event(event)

        if len(events) >= batch_size:
            continue

        task = await task_service.get(task_id)
        if task.status in _TERMINAL_STATUSES:
            # list_events() 读空后，终态事件可能恰好在 get() 之前提交。
            # 再排空一次游标后才关闭，避免丢失最后一批事件。
            terminal_events = await event_service.list_events(
                task_id,
                after_id=cursor,
                limit=batch_size,
            )
            if terminal_events:
                for event in terminal_events:
                    cursor = event.id
                    yield encode_sse_event(event)
                continue
            return
        if await request.is_disconnected():
            return

        now = monotonic()
        if now - last_heartbeat >= heartbeat_seconds:
            yield ": keep-alive\n\n"
            last_heartbeat = now
        await asyncio.sleep(poll_interval_seconds)
