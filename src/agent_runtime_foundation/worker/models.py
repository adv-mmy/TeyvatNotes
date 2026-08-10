"""Worker 领取、尝试记录和重试策略模型。"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from agent_runtime_foundation.domain import Task


class AttemptOutcome(StrEnum):
    """一次执行尝试的结果。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    RETRIES_EXHAUSTED = "retries_exhausted"
    PERMANENT_FAILURE = "permanent_failure"
    LEASE_EXPIRED = "lease_expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WorkClaim:
    """Worker 原子领取任务后获得的短期租约。"""

    task: Task
    attempt_id: UUID
    attempt_number: int
    attempt_in_cycle: int
    worker_id: str
    claimed_at: datetime
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """可用于诊断和恢复的单次 attempt 快照。"""

    task_id: UUID
    attempt_id: UUID
    attempt_number: int
    attempt_in_cycle: int
    worker_id: str
    started_at: datetime
    lease_expires_at: datetime
    finished_at: datetime | None = None
    outcome: AttemptOutcome = AttemptOutcome.RUNNING
    error: str | None = None
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """最大尝试次数与带抖动的指数退避参数。"""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        if not math.isfinite(self.base_delay_seconds) or self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds 必须大于 0")
        if not math.isfinite(self.max_delay_seconds):
            raise ValueError("max_delay_seconds 必须是有限数")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds 不能小于 base_delay_seconds")
        if not math.isfinite(self.jitter_ratio) or not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio 必须在 0 到 1 之间")


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """单个 Worker 的身份、轮询、租约、超时和重试配置。"""

    worker_id: str
    poll_interval_seconds: float = 0.1
    lease_duration_seconds: float = 30.0
    task_timeout_seconds: float = 20.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        if not self.worker_id.strip():
            raise ValueError("worker_id 不能为空")
        if not math.isfinite(self.poll_interval_seconds) or self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")
        if not math.isfinite(self.task_timeout_seconds) or self.task_timeout_seconds <= 0:
            raise ValueError("task_timeout_seconds 必须大于 0")
        if not math.isfinite(self.lease_duration_seconds):
            raise ValueError("lease_duration_seconds 必须是有限数")
        if self.lease_duration_seconds <= self.task_timeout_seconds:
            raise ValueError("lease_duration_seconds 必须大于 task_timeout_seconds")
