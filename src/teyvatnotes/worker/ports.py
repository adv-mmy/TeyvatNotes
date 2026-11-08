"""Worker 依赖的原子领取与 attempt 持久化端口。"""

from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from teyvatnotes.domain import JsonValue, Task
from teyvatnotes.worker.models import AttemptRecord, WorkClaim


class WorkerRepository(Protocol):
    """后续由 PostgreSQL 适配器实现的 Worker 存储端口。"""

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> WorkClaim | None:
        """原子领取一个到期的 queued 任务。"""

    async def complete_claim(
        self,
        claim: WorkClaim,
        *,
        output: JsonValue,
        now: datetime,
    ) -> Task:
        """仅允许当前未过期租约提交成功结果。"""

    async def fail_claim(
        self,
        claim: WorkClaim,
        *,
        error: str,
        retryable: bool,
        retry_at: datetime | None,
        now: datetime,
    ) -> Task:
        """保存失败，可重试时重新排队。"""

    async def recover_expired(self, *, now: datetime, max_attempts: int) -> int:
        """恢复过期租约，并收敛没有活动 attempt 的取消请求。"""

    async def list_attempts(self, task_id: UUID) -> tuple[AttemptRecord, ...]:
        """返回任务的全部执行尝试。"""
