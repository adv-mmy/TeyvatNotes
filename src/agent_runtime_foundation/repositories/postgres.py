"""基于 SQLAlchemy 2.x AsyncSession 的 PostgreSQL 任务仓储。"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime_foundation.application import TaskCreation
from agent_runtime_foundation.database import (
    TaskAttemptRecord,
    TaskEventRecord,
    TaskRecord,
)
from agent_runtime_foundation.domain import (
    JsonValue,
    Task,
    TaskResult,
    TaskStatus,
    TaskType,
)


def _as_json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_as_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _as_json_value(item) for key, item in value.items()}
    raise ValueError("数据库中存在无法转换为 JSON 的值")


def _to_record_values(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_type": task.task_type.value,
        "payload": task.payload,
        "status": task.status.value,
        "result": task.result.output if task.result is not None else None,
        "error": task.result.error if task.result is not None else None,
        "idempotency_key": task.idempotency_key,
        "version": task.version,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _to_domain(record: TaskRecord) -> Task:
    payload = _as_json_value(record.payload)
    if not isinstance(payload, dict):
        raise ValueError("tasks.payload 必须是 JSON 对象")

    status = TaskStatus(record.status)
    result = None
    if status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
        result = TaskResult(
            output=_as_json_value(record.result),
            error=record.error,
        )
    return Task.rehydrate(
        task_id=record.id,
        task_type=TaskType(record.task_type),
        payload=payload,
        idempotency_key=record.idempotency_key,
        status=status,
        result=result,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class PostgresTaskRepository:
    """用短生命周期 AsyncSession 实现事务化任务持久化。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, task: Task) -> TaskCreation:
        """在同一事务中写入 task 和 task_created 事件。"""

        async with self._session_factory.begin() as session:
            if task.idempotency_key is None:
                record = TaskRecord(**_to_record_values(task))
                session.add(record)
                await session.flush()
            else:
                statement = (
                    insert(TaskRecord)
                    .values(**_to_record_values(task))
                    .on_conflict_do_nothing(constraint="uq_tasks_idempotency_key")
                    .returning(TaskRecord.id)
                )
                inserted_id = await session.scalar(statement)
                if inserted_id is None:
                    existing = await session.scalar(
                        select(TaskRecord).where(TaskRecord.idempotency_key == task.idempotency_key)
                    )
                    if existing is None:
                        raise RuntimeError("幂等冲突后未找到原任务")
                    return TaskCreation(task=_to_domain(existing), created=False)
                inserted_record = await session.get(TaskRecord, inserted_id)
                if inserted_record is None:
                    raise RuntimeError("插入任务后未能重新读取")
                record = inserted_record

            session.add(
                TaskEventRecord(
                    task_id=record.id,
                    event_type="task_created",
                    from_status=None,
                    to_status=TaskStatus.QUEUED.value,
                    event_payload={"version": 0},
                    created_at=record.created_at,
                )
            )
            return TaskCreation(task=_to_domain(record), created=True)

    async def get(self, task_id: UUID) -> Task | None:
        async with self._session_factory() as session:
            record = await session.get(TaskRecord, task_id)
            return _to_domain(record) if record is not None else None

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

        filters = () if status is None else (TaskRecord.status == status.value,)
        async with self._session_factory() as session, session.begin():
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            total = await session.scalar(
                select(func.count()).select_from(TaskRecord).where(*filters)
            )
            records = (
                await session.scalars(
                    select(TaskRecord)
                    .where(*filters)
                    .order_by(TaskRecord.created_at, TaskRecord.id)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return [_to_domain(record) for record in records], int(total or 0)

    async def transition(
        self,
        task_id: UUID,
        target_status: TaskStatus,
        *,
        result: TaskResult | None = None,
        attempt_id: UUID | None = None,
    ) -> Task | None:
        """锁定任务行，并在一个事务中写状态、结果、attempt 和 event。"""

        async with self._session_factory.begin() as session:
            record = await session.scalar(
                select(TaskRecord).where(TaskRecord.id == task_id).with_for_update()
            )
            if record is None:
                return None

            from_status = TaskStatus(record.status)
            task = _to_domain(record)
            task.transition_to(target_status, result=result)
            self._update_record(record, task)
            await self._update_attempt(
                session,
                task=task,
                from_status=from_status,
                attempt_id=attempt_id,
            )
            session.add(
                TaskEventRecord(
                    task_id=task.id,
                    event_type="status_changed",
                    from_status=from_status.value,
                    to_status=task.status.value,
                    event_payload={"version": task.version},
                    created_at=task.updated_at,
                )
            )
            await session.flush()
            return _to_domain(record)

    async def explain_list_tasks(self, status: TaskStatus | None = None) -> JsonValue:
        """执行常用列表查询的 PostgreSQL JSON EXPLAIN。"""

        if status is None:
            statement = text(
                "EXPLAIN (FORMAT JSON) SELECT * FROM tasks ORDER BY created_at, id LIMIT 20"
            )
            parameters: dict[str, object] = {}
        else:
            statement = text(
                "EXPLAIN (FORMAT JSON) SELECT * FROM tasks "
                "WHERE status = :status ORDER BY created_at, id LIMIT 20"
            )
            parameters = {"status": status.value}

        async with self._session_factory() as session:
            plan = (await session.execute(statement, parameters)).scalar_one()
        return _as_json_value(plan)

    @staticmethod
    def _update_record(record: TaskRecord, task: Task) -> None:
        record.status = task.status.value
        record.result = task.result.output if task.result is not None else None
        record.error = task.result.error if task.result is not None else None
        record.version = task.version
        record.updated_at = task.updated_at

    @staticmethod
    async def _update_attempt(
        session: AsyncSession,
        *,
        task: Task,
        from_status: TaskStatus,
        attempt_id: UUID | None,
    ) -> None:
        if task.status is TaskStatus.RUNNING:
            current_max = await session.scalar(
                select(func.max(TaskAttemptRecord.attempt_number)).where(
                    TaskAttemptRecord.task_id == task.id
                )
            )
            session.add(
                TaskAttemptRecord(
                    id=attempt_id or uuid4(),
                    task_id=task.id,
                    attempt_number=(current_max or 0) + 1,
                    started_at=task.updated_at,
                    finished_at=None,
                    error=None,
                )
            )
            return

        if from_status not in {TaskStatus.RUNNING, TaskStatus.CANCEL_REQUESTED}:
            return
        if task.status not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return

        attempt_filter = [
            TaskAttemptRecord.task_id == task.id,
            TaskAttemptRecord.finished_at.is_(None),
        ]
        if attempt_id is not None:
            attempt_filter.append(TaskAttemptRecord.id == attempt_id)
        await session.execute(
            update(TaskAttemptRecord)
            .where(*attempt_filter)
            .values(
                finished_at=task.updated_at,
                error=task.result.error if task.result is not None else None,
            )
        )
