"""基于 SQLAlchemy 2.x AsyncSession 的 PostgreSQL 任务仓储。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime_foundation.application import (
    TaskCreation,
    TaskEvent,
    TaskEventType,
)
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
from agent_runtime_foundation.observability import current_request_id
from agent_runtime_foundation.worker import (
    AttemptOutcome,
    AttemptRecord,
    LeaseOwnershipError,
    WorkClaim,
)

type DatabaseClock = Callable[[AsyncSession], Awaitable[datetime]]


async def _database_now(session: AsyncSession) -> datetime:
    """从 PostgreSQL 获取租约权威时间，避免依赖 Worker 主机时钟。"""

    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("PostgreSQL 未返回有效当前时间")
    return value


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
        "available_at": task.created_at,
        "attempts_in_cycle": 0,
        "request_id": current_request_id(),
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


def _to_event(record: TaskEventRecord) -> TaskEvent:
    payload = _as_json_value(record.event_payload)
    if not isinstance(payload, dict):
        raise ValueError("task_events.payload 必须是 JSON 对象")
    return TaskEvent(
        id=record.id,
        task_id=record.task_id,
        event_type=TaskEventType(record.event_type),
        from_status=TaskStatus(record.from_status) if record.from_status is not None else None,
        to_status=TaskStatus(record.to_status) if record.to_status is not None else None,
        payload=payload,
        created_at=record.created_at,
    )


def _event_payload(
    task: Task,
    *,
    attempt_id: UUID | None = None,
    worker_id: str | None = None,
    request_id: str | None = None,
    progress: int | None = None,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {"version": task.version}
    resolved_request_id = request_id or current_request_id()
    if resolved_request_id is not None:
        payload["request_id"] = resolved_request_id
    if attempt_id is not None:
        payload["attempt_id"] = str(attempt_id)
    if worker_id is not None:
        payload["worker_id"] = worker_id
    if progress is not None:
        payload["progress"] = progress
    return payload


def _add_transition_events(
    session: AsyncSession,
    *,
    task: Task,
    from_status: TaskStatus,
    attempt_id: UUID | None = None,
    worker_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """把一次状态转换及其进度事件加入当前数据库事务。"""

    payload = _event_payload(
        task,
        attempt_id=attempt_id,
        worker_id=worker_id,
        request_id=request_id,
    )
    event_records: list[TaskEventRecord] = []
    if task.status is TaskStatus.SUCCEEDED:
        event_records.append(
            TaskEventRecord(
                task_id=task.id,
                event_type=TaskEventType.TASK_PROGRESS.value,
                from_status=from_status.value,
                to_status=task.status.value,
                event_payload={**payload, "progress": 100},
                created_at=task.updated_at,
            )
        )
    event_records.append(
        TaskEventRecord(
            task_id=task.id,
            event_type=TaskEventType.STATUS_CHANGED.value,
            from_status=from_status.value,
            to_status=task.status.value,
            event_payload=payload,
            created_at=task.updated_at,
        )
    )
    if task.status is TaskStatus.RUNNING:
        event_records.append(
            TaskEventRecord(
                task_id=task.id,
                event_type=TaskEventType.TASK_PROGRESS.value,
                from_status=from_status.value,
                to_status=task.status.value,
                event_payload={**payload, "progress": 0},
                created_at=task.updated_at,
            )
        )
    session.add_all(event_records)


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
                    event_payload=_event_payload(task),
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

    async def list_events(
        self,
        task_id: UUID,
        *,
        after_id: int,
        limit: int,
    ) -> tuple[TaskEvent, ...] | None:
        """按全局递增事件 ID 读取任务事件。"""

        if after_id < 0:
            raise ValueError("after_id 不能为负数")
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        async with self._session_factory() as session:
            task_exists = await session.scalar(
                select(TaskRecord.id).where(TaskRecord.id == task_id)
            )
            if task_exists is None:
                return None
            records = (
                await session.scalars(
                    select(TaskEventRecord)
                    .where(
                        TaskEventRecord.task_id == task_id,
                        TaskEventRecord.id > after_id,
                    )
                    .order_by(TaskEventRecord.id)
                    .limit(limit)
                )
            ).all()
        return tuple(_to_event(record) for record in records)

    async def is_ready(self) -> bool:
        """验证连接与第六周运行必需的任务、事件表均可访问。"""

        async with self._session_factory() as session:
            await session.execute(
                select(
                    TaskRecord.id,
                    TaskRecord.available_at,
                    TaskRecord.attempts_in_cycle,
                ).limit(1)
            )
            await session.execute(
                select(
                    TaskAttemptRecord.id,
                    TaskAttemptRecord.worker_id,
                    TaskAttemptRecord.lease_expires_at,
                    TaskAttemptRecord.outcome,
                ).limit(1)
            )
            await session.execute(select(TaskEventRecord.id).limit(1))
        return True

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
            self._update_record(record, task, from_status=from_status)
            await self._update_attempt(
                session,
                task=task,
                from_status=from_status,
                attempt_id=attempt_id,
                attempt_in_cycle=record.attempts_in_cycle,
                request_id=record.request_id,
            )
            _add_transition_events(
                session,
                task=task,
                from_status=from_status,
                attempt_id=attempt_id,
                request_id=record.request_id,
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
    def _update_record(
        record: TaskRecord,
        task: Task,
        *,
        from_status: TaskStatus,
        reset_retry_cycle: bool = True,
    ) -> None:
        record.status = task.status.value
        record.result = task.result.output if task.result is not None else None
        record.error = task.result.error if task.result is not None else None
        record.version = task.version
        record.updated_at = task.updated_at
        if task.status is TaskStatus.RUNNING and from_status is TaskStatus.QUEUED:
            record.attempts_in_cycle += 1
        if task.status is TaskStatus.QUEUED:
            record.available_at = task.updated_at
            if from_status is TaskStatus.FAILED and reset_retry_cycle:
                record.attempts_in_cycle = 0

    @staticmethod
    async def _update_attempt(
        session: AsyncSession,
        *,
        task: Task,
        from_status: TaskStatus,
        attempt_id: UUID | None,
        attempt_in_cycle: int,
        request_id: str | None,
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
                    attempt_in_cycle=attempt_in_cycle,
                    worker_id="embedded-runtime",
                    started_at=task.updated_at,
                    lease_expires_at=task.updated_at + timedelta(days=3650),
                    finished_at=None,
                    outcome=AttemptOutcome.RUNNING.value,
                    error=None,
                    retry_at=None,
                    request_id=request_id,
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
                outcome={
                    TaskStatus.SUCCEEDED: AttemptOutcome.SUCCEEDED.value,
                    TaskStatus.CANCELLED: AttemptOutcome.CANCELLED.value,
                    TaskStatus.FAILED: AttemptOutcome.PERMANENT_FAILURE.value,
                }[task.status],
                error=task.result.error if task.result is not None else None,
            )
        )


class PostgresWorkerRepository:
    """用 PostgreSQL 行锁和租约实现跨进程 WorkerRepository。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: DatabaseClock = _database_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> WorkClaim | None:
        """通过 FOR UPDATE SKIP LOCKED 原子领取一个可执行任务。"""

        if not worker_id.strip():
            raise ValueError("worker_id 不能为空")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration 必须大于 0")

        async with self._session_factory.begin() as session:
            scan_now = await self._clock(session)
            record = await session.scalar(
                select(TaskRecord)
                .where(
                    TaskRecord.status == TaskStatus.QUEUED.value,
                    TaskRecord.available_at <= scan_now,
                )
                .order_by(TaskRecord.available_at, TaskRecord.created_at, TaskRecord.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if record is None:
                return None
            database_now = await self._clock(session)
            if record.available_at > database_now:
                return None

            attempt_number = (
                await session.scalar(
                    select(func.max(TaskAttemptRecord.attempt_number)).where(
                        TaskAttemptRecord.task_id == record.id
                    )
                )
                or 0
            ) + 1
            attempt_id = uuid4()
            task = self._transition_at(
                session,
                record,
                TaskStatus.RUNNING,
                now=database_now,
                attempt_id=attempt_id,
                worker_id=worker_id,
            )
            lease_expires_at = database_now + lease_duration
            session.add(
                TaskAttemptRecord(
                    id=attempt_id,
                    task_id=record.id,
                    attempt_number=attempt_number,
                    attempt_in_cycle=record.attempts_in_cycle,
                    worker_id=worker_id,
                    started_at=database_now,
                    lease_expires_at=lease_expires_at,
                    finished_at=None,
                    outcome=AttemptOutcome.RUNNING.value,
                    error=None,
                    retry_at=None,
                    request_id=record.request_id,
                )
            )
            await session.flush()
            return WorkClaim(
                task=task,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                attempt_in_cycle=record.attempts_in_cycle,
                worker_id=worker_id,
                claimed_at=database_now,
                lease_expires_at=lease_expires_at,
                request_id=record.request_id,
            )

    async def complete_claim(
        self,
        claim: WorkClaim,
        *,
        output: JsonValue,
        now: datetime,
    ) -> Task:
        """仅让数据库中仍持有有效租约的 Worker 提交结果。"""

        async with self._session_factory.begin() as session:
            record, attempt = await self._owned_records(session, claim)
            database_now = await self._clock(session)
            self._validate_unexpired(attempt, now=database_now)
            status = TaskStatus(record.status)
            if status is TaskStatus.CANCEL_REQUESTED:
                task = self._transition_at(
                    session,
                    record,
                    TaskStatus.CANCELLED,
                    now=database_now,
                    attempt_id=claim.attempt_id,
                    worker_id=claim.worker_id,
                )
                outcome = AttemptOutcome.CANCELLED
            elif status is TaskStatus.RUNNING:
                task = self._transition_at(
                    session,
                    record,
                    TaskStatus.SUCCEEDED,
                    now=database_now,
                    result=TaskResult(output=output),
                    attempt_id=claim.attempt_id,
                    worker_id=claim.worker_id,
                )
                outcome = AttemptOutcome.SUCCEEDED
            else:
                raise LeaseOwnershipError("任务已不在当前 attempt 的可完成状态")

            self._finish_attempt(attempt, now=database_now, outcome=outcome)
            await session.flush()
            return task

    async def fail_claim(
        self,
        claim: WorkClaim,
        *,
        error: str,
        retryable: bool,
        retry_at: datetime | None,
        now: datetime,
    ) -> Task:
        """原子保存失败，并在需要时为同一重试周期重新排队。"""

        if not retryable and retry_at is not None:
            raise ValueError("永久错误不能设置 retry_at")

        async with self._session_factory.begin() as session:
            record, attempt = await self._owned_records(session, claim)
            database_now = await self._clock(session)
            self._validate_unexpired(attempt, now=database_now)
            database_retry_at = (
                database_now + max(retry_at - now, timedelta(0)) if retry_at is not None else None
            )
            status = TaskStatus(record.status)
            if status is TaskStatus.CANCEL_REQUESTED:
                task = self._transition_at(
                    session,
                    record,
                    TaskStatus.CANCELLED,
                    now=database_now,
                    attempt_id=claim.attempt_id,
                    worker_id=claim.worker_id,
                )
                outcome = AttemptOutcome.CANCELLED
                database_retry_at = None
            elif status is TaskStatus.RUNNING:
                task = self._transition_at(
                    session,
                    record,
                    TaskStatus.FAILED,
                    now=database_now,
                    result=TaskResult(error=error),
                    attempt_id=claim.attempt_id,
                    worker_id=claim.worker_id,
                )
                if database_retry_at is None:
                    outcome = (
                        AttemptOutcome.RETRIES_EXHAUSTED
                        if retryable
                        else AttemptOutcome.PERMANENT_FAILURE
                    )
                else:
                    task = self._transition_at(
                        session,
                        record,
                        TaskStatus.QUEUED,
                        now=database_now,
                        attempt_id=claim.attempt_id,
                        worker_id=claim.worker_id,
                        reset_retry_cycle=False,
                    )
                    record.available_at = database_retry_at
                    outcome = AttemptOutcome.RETRY_SCHEDULED
            else:
                raise LeaseOwnershipError("任务已不在当前 attempt 的可失败状态")

            self._finish_attempt(
                attempt,
                now=database_now,
                outcome=outcome,
                error=error,
                retry_at=database_retry_at,
            )
            await session.flush()
            return task

    async def recover_expired(self, *, now: datetime, max_attempts: int) -> int:
        """锁定过期租约并重排任务，同时收敛无活动 attempt 的取消请求。"""

        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")

        recovered = 0
        async with self._session_factory.begin() as session:
            scan_now = await self._clock(session)
            expired_tasks = (
                await session.scalars(
                    select(TaskRecord)
                    .join(TaskAttemptRecord, TaskAttemptRecord.task_id == TaskRecord.id)
                    .where(
                        TaskAttemptRecord.finished_at.is_(None),
                        TaskAttemptRecord.outcome == AttemptOutcome.RUNNING.value,
                        TaskAttemptRecord.lease_expires_at <= scan_now,
                    )
                    .order_by(TaskAttemptRecord.lease_expires_at, TaskRecord.id)
                    .with_for_update(of=TaskRecord, skip_locked=True)
                )
            ).all()
            for record in expired_tasks:
                attempt = await session.scalar(
                    select(TaskAttemptRecord)
                    .where(
                        TaskAttemptRecord.task_id == record.id,
                        TaskAttemptRecord.finished_at.is_(None),
                        TaskAttemptRecord.outcome == AttemptOutcome.RUNNING.value,
                    )
                    .with_for_update()
                )
                if attempt is None:
                    continue
                database_now = await self._clock(session)
                if attempt.lease_expires_at > database_now:
                    continue

                status = TaskStatus(record.status)
                retry_at: datetime | None = None
                if status is TaskStatus.CANCEL_REQUESTED:
                    self._transition_at(
                        session,
                        record,
                        TaskStatus.CANCELLED,
                        now=database_now,
                        attempt_id=attempt.id,
                        worker_id=attempt.worker_id,
                    )
                    outcome = AttemptOutcome.CANCELLED
                elif status is TaskStatus.RUNNING:
                    self._transition_at(
                        session,
                        record,
                        TaskStatus.FAILED,
                        now=database_now,
                        result=TaskResult(error="Worker 租约过期"),
                        attempt_id=attempt.id,
                        worker_id=attempt.worker_id,
                    )
                    outcome = AttemptOutcome.LEASE_EXPIRED
                    if attempt.attempt_in_cycle < max_attempts:
                        self._transition_at(
                            session,
                            record,
                            TaskStatus.QUEUED,
                            now=database_now,
                            attempt_id=attempt.id,
                            worker_id=attempt.worker_id,
                            reset_retry_cycle=False,
                        )
                        record.available_at = database_now
                        retry_at = database_now
                else:
                    outcome = AttemptOutcome.LEASE_EXPIRED

                self._finish_attempt(
                    attempt,
                    now=database_now,
                    outcome=outcome,
                    error="Worker 租约过期",
                    retry_at=retry_at,
                )
                recovered += 1

            active_attempt_exists = exists().where(
                TaskAttemptRecord.task_id == TaskRecord.id,
                TaskAttemptRecord.finished_at.is_(None),
            )
            abandoned_cancellations = (
                await session.scalars(
                    select(TaskRecord)
                    .where(
                        TaskRecord.status == TaskStatus.CANCEL_REQUESTED.value,
                        ~active_attempt_exists,
                    )
                    .order_by(TaskRecord.created_at, TaskRecord.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            cancellation_now = await self._clock(session)
            for record in abandoned_cancellations:
                self._transition_at(
                    session,
                    record,
                    TaskStatus.CANCELLED,
                    now=cancellation_now,
                )
                recovered += 1

            await session.flush()
        return recovered

    async def list_attempts(self, task_id: UUID) -> tuple[AttemptRecord, ...]:
        """从 PostgreSQL 返回全局 attempt 序号稳定的执行历史。"""

        async with self._session_factory() as session:
            records = (
                await session.scalars(
                    select(TaskAttemptRecord)
                    .where(TaskAttemptRecord.task_id == task_id)
                    .order_by(TaskAttemptRecord.attempt_number)
                )
            ).all()
        return tuple(
            AttemptRecord(
                task_id=record.task_id,
                attempt_id=record.id,
                attempt_number=record.attempt_number,
                attempt_in_cycle=record.attempt_in_cycle,
                worker_id=record.worker_id,
                started_at=record.started_at,
                lease_expires_at=record.lease_expires_at,
                finished_at=record.finished_at,
                outcome=AttemptOutcome(record.outcome),
                error=record.error,
                retry_at=record.retry_at,
            )
            for record in records
        )

    @staticmethod
    async def _owned_records(
        session: AsyncSession,
        claim: WorkClaim,
    ) -> tuple[TaskRecord, TaskAttemptRecord]:
        record = await session.scalar(
            select(TaskRecord).where(TaskRecord.id == claim.task.id).with_for_update()
        )
        attempt = await session.scalar(
            select(TaskAttemptRecord)
            .where(TaskAttemptRecord.id == claim.attempt_id)
            .with_for_update()
        )
        if record is None or attempt is None:
            raise LeaseOwnershipError("任务没有活动租约")
        if attempt.task_id != record.id or attempt.worker_id != claim.worker_id:
            raise LeaseOwnershipError("当前 Worker 不拥有该 attempt")
        if attempt.finished_at is not None or attempt.outcome != AttemptOutcome.RUNNING.value:
            raise LeaseOwnershipError("任务没有活动租约")
        return record, attempt

    @staticmethod
    def _validate_unexpired(attempt: TaskAttemptRecord, *, now: datetime) -> None:
        """在任务与 attempt 行锁均已获取后，以数据库实时时钟校验租约。"""

        if attempt.lease_expires_at <= now:
            raise LeaseOwnershipError("该 attempt 租约已过期")

    @staticmethod
    def _transition_at(
        session: AsyncSession,
        record: TaskRecord,
        target_status: TaskStatus,
        *,
        now: datetime,
        result: TaskResult | None = None,
        attempt_id: UUID | None = None,
        worker_id: str | None = None,
        reset_retry_cycle: bool = True,
    ) -> Task:
        from_status = TaskStatus(record.status)
        task = _to_domain(record)
        task.transition_to(target_status, result=result)
        task.updated_at = now
        PostgresTaskRepository._update_record(
            record,
            task,
            from_status=from_status,
            reset_retry_cycle=reset_retry_cycle,
        )
        _add_transition_events(
            session,
            task=task,
            from_status=from_status,
            attempt_id=attempt_id,
            worker_id=worker_id,
            request_id=record.request_id,
        )
        return task

    @staticmethod
    def _finish_attempt(
        attempt: TaskAttemptRecord,
        *,
        now: datetime,
        outcome: AttemptOutcome,
        error: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        attempt.finished_at = now
        attempt.outcome = outcome.value
        attempt.error = error
        attempt.retry_at = retry_at
