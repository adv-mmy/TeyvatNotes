"""线程安全的内存任务仓储。"""

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from agent_runtime_foundation.application import TaskCreation
from agent_runtime_foundation.domain import JsonValue, Task, TaskResult, TaskStatus
from agent_runtime_foundation.worker.errors import LeaseOwnershipError
from agent_runtime_foundation.worker.models import (
    AttemptOutcome,
    AttemptRecord,
    WorkClaim,
)


@dataclass(slots=True)
class _WorkState:
    """内存仓储中不暴露给领域对象的调度状态。"""

    available_at: datetime
    attempt_count: int = 0
    attempts_in_cycle: int = 0
    active_claim: WorkClaim | None = None


class InMemoryTaskRepository:
    """使用字典保存任务，用于开发和 API 测试。"""

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._idempotency_keys: dict[str, UUID] = {}
        self._work_states: dict[UUID, _WorkState] = {}
        self._attempts: dict[UUID, list[AttemptRecord]] = {}
        self._lock = RLock()

    async def add(self, task: Task) -> TaskCreation:
        with self._lock:
            if task.id in self._tasks:
                raise ValueError(f"任务 {task.id} 已存在")
            if task.idempotency_key is not None:
                existing_id = self._idempotency_keys.get(task.idempotency_key)
                if existing_id is not None:
                    return TaskCreation(task=deepcopy(self._tasks[existing_id]), created=False)
            self._tasks[task.id] = deepcopy(task)
            self._work_states[task.id] = _WorkState(available_at=task.created_at)
            self._attempts[task.id] = []
            if task.idempotency_key is not None:
                self._idempotency_keys[task.idempotency_key] = task.id
            return TaskCreation(task=deepcopy(task), created=True)

    async def get(self, task_id: UUID) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return deepcopy(task) if task is not None else None

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

        with self._lock:
            tasks = [
                task for task in self._tasks.values() if status is None or task.status is status
            ]
            tasks.sort(key=lambda task: (task.created_at, task.id.hex))
            total = len(tasks)
            return deepcopy(tasks[offset : offset + limit]), total

    async def transition(
        self,
        task_id: UUID,
        target_status: TaskStatus,
        *,
        result: TaskResult | None = None,
        attempt_id: UUID | None = None,
    ) -> Task | None:
        del attempt_id
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.transition_to(target_status, result=deepcopy(result))
            if target_status is TaskStatus.QUEUED:
                state = self._work_states[task_id]
                state.available_at = task.updated_at
                # 只有显式的 failed -> queued 手动重试经过此处。
                # fail_claim() 内部的自动重试不重置该轮预算。
                state.attempts_in_cycle = 0
            return deepcopy(task)

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> WorkClaim | None:
        """在同一临界区中选择 queued 任务、转为 running 并创建 attempt。"""

        if not worker_id.strip():
            raise ValueError("worker_id 不能为空")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration 必须大于 0")

        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda task: (task.created_at, task.id.hex),
            )
            for task in tasks:
                state = self._work_states[task.id]
                if task.status is not TaskStatus.QUEUED:
                    continue
                if state.active_claim is not None or state.available_at > now:
                    continue

                task.transition_to(TaskStatus.RUNNING)
                state.attempt_count += 1
                state.attempts_in_cycle += 1
                attempt_id = uuid4()
                claim = WorkClaim(
                    task=deepcopy(task),
                    attempt_id=attempt_id,
                    attempt_number=state.attempt_count,
                    attempt_in_cycle=state.attempts_in_cycle,
                    worker_id=worker_id,
                    claimed_at=now,
                    lease_expires_at=now + lease_duration,
                )
                state.active_claim = claim
                self._attempts[task.id].append(
                    AttemptRecord(
                        task_id=task.id,
                        attempt_id=attempt_id,
                        attempt_number=state.attempt_count,
                        attempt_in_cycle=state.attempts_in_cycle,
                        worker_id=worker_id,
                        started_at=now,
                        lease_expires_at=claim.lease_expires_at,
                    )
                )
                return deepcopy(claim)
        return None

    async def complete_claim(
        self,
        claim: WorkClaim,
        *,
        output: JsonValue,
        now: datetime,
    ) -> Task:
        """提交成功结果；已请求取消的任务收敛为 cancelled。"""

        with self._lock:
            state, task = self._owned_claim(claim, now=now)
            if task.status is TaskStatus.CANCEL_REQUESTED:
                task.transition_to(TaskStatus.CANCELLED)
                outcome = AttemptOutcome.CANCELLED
            elif task.status is TaskStatus.RUNNING:
                task.transition_to(
                    TaskStatus.SUCCEEDED,
                    result=TaskResult(output=deepcopy(output)),
                )
                outcome = AttemptOutcome.SUCCEEDED
            else:
                raise LeaseOwnershipError("任务已不在当前 attempt 的可完成状态")

            self._finish_attempt(
                task.id,
                claim.attempt_id,
                finished_at=now,
                outcome=outcome,
            )
            state.active_claim = None
            return deepcopy(task)

    async def fail_claim(
        self,
        claim: WorkClaim,
        *,
        error: str,
        retryable: bool,
        retry_at: datetime | None,
        now: datetime,
    ) -> Task:
        """保存尝试失败，可重试时原子地恢复为 queued。"""

        with self._lock:
            state, task = self._owned_claim(claim, now=now)
            if task.status is TaskStatus.CANCEL_REQUESTED:
                task.transition_to(TaskStatus.CANCELLED)
                outcome = AttemptOutcome.CANCELLED
                retry_at = None
            elif task.status is TaskStatus.RUNNING:
                task.transition_to(TaskStatus.FAILED, result=TaskResult(error=error))
                if retry_at is None:
                    outcome = (
                        AttemptOutcome.RETRIES_EXHAUSTED
                        if retryable
                        else AttemptOutcome.PERMANENT_FAILURE
                    )
                else:
                    task.transition_to(TaskStatus.QUEUED)
                    state.available_at = retry_at
                    outcome = AttemptOutcome.RETRY_SCHEDULED
            else:
                raise LeaseOwnershipError("任务已不在当前 attempt 的可失败状态")

            self._finish_attempt(
                task.id,
                claim.attempt_id,
                finished_at=now,
                outcome=outcome,
                error=error,
                retry_at=retry_at,
            )
            state.active_claim = None
            return deepcopy(task)

    async def recover_expired(self, *, now: datetime, max_attempts: int) -> int:
        """把租约过期的 running 任务重新排队或结束为失败。"""

        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")

        recovered = 0
        with self._lock:
            for task_id, state in self._work_states.items():
                claim = state.active_claim
                task = self._tasks[task_id]
                if claim is None:
                    if task.status is TaskStatus.CANCEL_REQUESTED:
                        task.transition_to(TaskStatus.CANCELLED)
                        recovered += 1
                    continue
                if claim.lease_expires_at > now:
                    continue

                if task.status is TaskStatus.CANCEL_REQUESTED:
                    task.transition_to(TaskStatus.CANCELLED)
                    outcome = AttemptOutcome.CANCELLED
                elif task.status is TaskStatus.RUNNING:
                    task.transition_to(
                        TaskStatus.FAILED,
                        result=TaskResult(error="Worker 租约过期"),
                    )
                    outcome = AttemptOutcome.LEASE_EXPIRED
                    if claim.attempt_in_cycle < max_attempts:
                        task.transition_to(TaskStatus.QUEUED)
                        state.available_at = now
                else:
                    outcome = AttemptOutcome.LEASE_EXPIRED

                retry_at = (
                    now
                    if task.status is TaskStatus.QUEUED and claim.attempt_in_cycle < max_attempts
                    else None
                )
                self._finish_attempt(
                    task_id,
                    claim.attempt_id,
                    finished_at=now,
                    outcome=outcome,
                    error="Worker 租约过期",
                    retry_at=retry_at,
                )
                state.active_claim = None
                recovered += 1
        return recovered

    async def list_attempts(self, task_id: UUID) -> tuple[AttemptRecord, ...]:
        """返回按执行顺序保存的 attempt 快照。"""

        with self._lock:
            return tuple(deepcopy(self._attempts.get(task_id, [])))

    def _owned_claim(self, claim: WorkClaim, *, now: datetime) -> tuple[_WorkState, Task]:
        state = self._work_states.get(claim.task.id)
        if state is None or state.active_claim is None:
            raise LeaseOwnershipError("任务没有活动租约")
        active = state.active_claim
        if active.attempt_id != claim.attempt_id or active.worker_id != claim.worker_id:
            raise LeaseOwnershipError("当前 Worker 不拥有该 attempt")
        if active.lease_expires_at <= now:
            raise LeaseOwnershipError("该 attempt 租约已过期")
        return state, self._tasks[claim.task.id]

    def _finish_attempt(
        self,
        task_id: UUID,
        attempt_id: UUID,
        *,
        finished_at: datetime,
        outcome: AttemptOutcome,
        error: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        attempts = self._attempts[task_id]
        for index, attempt in enumerate(attempts):
            if attempt.attempt_id == attempt_id:
                attempts[index] = replace(
                    attempt,
                    finished_at=finished_at,
                    outcome=outcome,
                    error=error,
                    retry_at=retry_at,
                )
                return
        raise LeaseOwnershipError("未找到租约对应的 attempt")
