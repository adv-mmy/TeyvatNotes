"""进程内任务成功率、耗时与失败类型统计。"""

import math
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class DurationSnapshot:
    """已完成任务的耗时聚合。"""

    count: int
    total_seconds: float
    average_seconds: float
    max_seconds: float


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """任务运行指标的一致快照。"""

    started: int
    completed: int
    succeeded: int
    failed: int
    cancelled: int
    success_rate: float
    duration: DurationSnapshot
    failure_types: dict[str, int]


class MetricsRegistry:
    """使用短临界区记录进程内任务指标。"""

    def __init__(self) -> None:
        self._started = 0
        self._succeeded = 0
        self._failed = 0
        self._cancelled = 0
        self._duration_total = 0.0
        self._duration_max = 0.0
        self._failure_types: dict[str, int] = {}
        self._lock = Lock()

    def record_started(self) -> None:
        """记录一次真正进入执行器的 attempt。"""

        with self._lock:
            self._started += 1

    def record_completed(
        self,
        *,
        outcome: str,
        duration_seconds: float,
        failure_type: str | None = None,
    ) -> None:
        """记录终态、耗时和可选的失败分类。"""

        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("duration_seconds 必须是非负有限数")
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("不支持的 outcome")

        with self._lock:
            if outcome == "succeeded":
                self._succeeded += 1
            elif outcome == "failed":
                self._failed += 1
            else:
                self._cancelled += 1
            self._duration_total += duration_seconds
            self._duration_max = max(self._duration_max, duration_seconds)
            if outcome == "failed" and failure_type is not None:
                self._failure_types[failure_type] = self._failure_types.get(failure_type, 0) + 1

    def snapshot(self) -> MetricsSnapshot:
        """返回不暴露内部可变容器的快照。"""

        with self._lock:
            completed = self._succeeded + self._failed + self._cancelled
            average = self._duration_total / completed if completed else 0.0
            success_rate = self._succeeded / completed if completed else 0.0
            return MetricsSnapshot(
                started=self._started,
                completed=completed,
                succeeded=self._succeeded,
                failed=self._failed,
                cancelled=self._cancelled,
                success_rate=success_rate,
                duration=DurationSnapshot(
                    count=completed,
                    total_seconds=self._duration_total,
                    average_seconds=average,
                    max_seconds=self._duration_max,
                ),
                failure_types=dict(self._failure_types),
            )
