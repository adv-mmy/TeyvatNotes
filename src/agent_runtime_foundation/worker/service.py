"""独立 Worker 的单次处理和长期轮询逻辑。"""

import asyncio
import logging
import random
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from time import perf_counter

from agent_runtime_foundation.domain import TaskStatus
from agent_runtime_foundation.observability import MetricsRegistry, bind_correlation_context
from agent_runtime_foundation.runtime import TaskExecutionError, TaskExecutor
from agent_runtime_foundation.worker.errors import PermanentTaskError, RetryableTaskError
from agent_runtime_foundation.worker.models import WorkClaim, WorkerSettings
from agent_runtime_foundation.worker.ports import WorkerRepository
from agent_runtime_foundation.worker.retry import calculate_retry_delay

logger = logging.getLogger(__name__)

type Clock = Callable[[], datetime]
type RandomSource = Callable[[], float]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TaskWorker:
    """从 WorkerRepository 领取任务并执行有界重试。"""

    def __init__(
        self,
        repository: WorkerRepository,
        executor: TaskExecutor,
        settings: WorkerSettings,
        *,
        clock: Clock = _utc_now,
        random_source: RandomSource = random.random,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._settings = settings
        self._clock = clock
        self._random_source = random_source
        self._metrics = metrics or MetricsRegistry()

    async def run_once(self) -> bool:
        """恢复过期任务并最多执行一个 claim。"""

        now = self._clock()
        await self._repository.recover_expired(
            now=now,
            max_attempts=self._settings.retry_policy.max_attempts,
        )
        claim = await self._repository.claim_next(
            worker_id=self._settings.worker_id,
            now=now,
            lease_duration=timedelta(seconds=self._settings.lease_duration_seconds),
        )
        if claim is None:
            return False

        await self._execute_claim(claim)
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """持续轮询，无任务时等待停止信号或下一轮周期。"""

        while not stop_event.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Worker 本轮处理失败",
                    extra={
                        "event": "worker_iteration_failed",
                        "exception_type": type(exc).__name__,
                    },
                )
                processed = False
            if processed:
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._settings.poll_interval_seconds,
                )

    async def _execute_claim(self, claim: WorkClaim) -> None:
        with bind_correlation_context(
            request_id=claim.request_id,
            task_id=claim.task.id,
            attempt_id=claim.attempt_id,
            worker_id=claim.worker_id,
            inherit=False,
        ):
            started_at = perf_counter()
            outcome = "failed"
            failure_type: str | None = None
            interrupted = False
            self._metrics.record_started()
            logger.info("任务开始执行", extra={"event": "task_started"})
            try:
                async with asyncio.timeout(self._settings.task_timeout_seconds):
                    output = await self._executor.execute(claim.task)
            except asyncio.CancelledError:
                # Worker 被强制结束时不伪造完成记录，由租约过期恢复。
                interrupted = True
                raise
            except (PermanentTaskError, TaskExecutionError) as exc:
                failure_type = type(exc).__name__
                logger.warning(
                    "任务发生永久错误",
                    extra={"event": "task_failed", "failure_type": failure_type},
                )
                final_status = await self._record_failure(
                    claim,
                    error="任务无法执行",
                    retryable=False,
                )
                outcome = self._metric_outcome(final_status, default="failed")
            except RetryableTaskError as exc:
                failure_type = type(exc).__name__
                logger.warning(
                    "任务发生可重试错误",
                    extra={"event": "task_failed", "failure_type": failure_type},
                )
                final_status = await self._record_failure(
                    claim,
                    error="任务暂时无法执行",
                    retryable=True,
                )
                outcome = self._metric_outcome(final_status, default="failed")
            except TimeoutError:
                failure_type = "timeout"
                final_status = await self._record_failure(
                    claim,
                    error="任务执行超时",
                    retryable=True,
                )
                outcome = self._metric_outcome(final_status, default="failed")
            except Exception as exc:
                failure_type = "unexpected_error"
                logger.error(
                    "任务发生未预期执行错误",
                    extra={
                        "event": "task_failed",
                        "failure_type": failure_type,
                        "exception_type": type(exc).__name__,
                    },
                )
                final_status = await self._record_failure(
                    claim,
                    error="任务执行失败",
                    retryable=True,
                )
                outcome = self._metric_outcome(final_status, default="failed")
            else:
                completed = await self._repository.complete_claim(
                    claim,
                    output=output,
                    now=self._clock(),
                )
                outcome = self._metric_outcome(completed.status, default="succeeded")
            finally:
                duration = perf_counter() - started_at
                if interrupted:
                    logger.warning(
                        "Worker 执行被中断，等待租约恢复",
                        extra={
                            "event": "task_abandoned",
                            "duration_ms": round(duration * 1000, 3),
                        },
                    )
                else:
                    self._metrics.record_completed(
                        outcome=outcome,
                        duration_seconds=duration,
                        failure_type=failure_type,
                    )
                    logger.info(
                        "任务执行结束",
                        extra={
                            "event": "task_completed",
                            "task_status": outcome,
                            "duration_ms": round(duration * 1000, 3),
                            "failure_type": failure_type,
                        },
                    )

    async def _record_failure(
        self,
        claim: WorkClaim,
        *,
        error: str,
        retryable: bool,
    ) -> TaskStatus:
        now = self._clock()
        retry_at = None
        if retryable and claim.attempt_in_cycle < self._settings.retry_policy.max_attempts:
            delay = calculate_retry_delay(
                self._settings.retry_policy,
                attempt_number=claim.attempt_in_cycle,
                random_value=self._random_source(),
            )
            retry_at = now + timedelta(seconds=delay)

        task = await self._repository.fail_claim(
            claim,
            error=error,
            retryable=retryable,
            retry_at=retry_at,
            now=now,
        )
        return task.status

    @staticmethod
    def _metric_outcome(status: TaskStatus, *, default: str) -> str:
        """取消竞态获胜时以最终持久化状态修正指标。"""

        return "cancelled" if status is TaskStatus.CANCELLED else default
