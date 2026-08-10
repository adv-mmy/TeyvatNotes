"""独立 Worker 的单次处理和长期轮询逻辑。"""

import asyncio
import logging
import random
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

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
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._settings = settings
        self._clock = clock
        self._random_source = random_source

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
            except Exception:
                logger.exception("Worker %s 本轮处理失败", self._settings.worker_id)
                processed = False
            if processed:
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._settings.poll_interval_seconds,
                )

    async def _execute_claim(self, claim: WorkClaim) -> None:
        try:
            async with asyncio.timeout(self._settings.task_timeout_seconds):
                output = await self._executor.execute(claim.task)
        except asyncio.CancelledError:
            # Worker 被强制结束时不伪造完成记录，由租约过期恢复。
            raise
        except (PermanentTaskError, TaskExecutionError) as exc:
            logger.warning(
                "任务 %s 发生永久错误（%s）",
                claim.task.id,
                type(exc).__name__,
            )
            await self._record_failure(claim, error="任务无法执行", retryable=False)
        except RetryableTaskError as exc:
            logger.warning(
                "任务 %s 发生可重试错误（%s）",
                claim.task.id,
                type(exc).__name__,
            )
            await self._record_failure(claim, error="任务暂时无法执行", retryable=True)
        except TimeoutError:
            await self._record_failure(claim, error="任务执行超时", retryable=True)
        except Exception:
            logger.exception("任务 %s 发生未预期执行错误", claim.task.id)
            await self._record_failure(claim, error="任务执行失败", retryable=True)
        else:
            await self._repository.complete_claim(
                claim,
                output=output,
                now=self._clock(),
            )

    async def _record_failure(
        self,
        claim: WorkClaim,
        *,
        error: str,
        retryable: bool,
    ) -> None:
        now = self._clock()
        retry_at = None
        if retryable and claim.attempt_in_cycle < self._settings.retry_policy.max_attempts:
            delay = calculate_retry_delay(
                self._settings.retry_policy,
                attempt_number=claim.attempt_in_cycle,
                random_value=self._random_source(),
            )
            retry_at = now + timedelta(seconds=delay)

        await self._repository.fail_claim(
            claim,
            error=error,
            retryable=retryable,
            retry_at=retry_at,
            now=now,
        )
