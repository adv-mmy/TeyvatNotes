"""单次自然语言问答的共享账本；只存元数据，不跨请求复用。"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Literal

from teyvatnotes.application.model_ports import ModelError, ModelUsage
from teyvatnotes.domain.task import JsonValue

Stage = Literal["understanding", "answer"]


@dataclass(frozen=True, slots=True)
class GenerationAttempt:
    stage: Stage
    usage: ModelUsage = ModelUsage(None, None)
    model: str | None = None
    elapsed_ms: int | None = None


class GenerationLedger:
    """同步领取发生在await前；未知用量保留None，已知Token只是事后软阈值。"""

    def __init__(
        self,
        *,
        max_calls: int = 4,
        max_reported_tokens: int = 20_000,
        timeout_seconds: float = 90,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        if (
            type(max_calls) is not int
            or not 1 <= max_calls <= 4
            or type(max_reported_tokens) is not int
            or not 1 <= max_reported_tokens <= 20_000
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 90
        ):
            raise ModelError("invalid_generation_budget")
        self._max_calls, self._max_tokens = max_calls, max_reported_tokens
        self._timeout, self._clock = timeout_seconds, clock
        self._started = clock()
        self._attempts: list[GenerationAttempt] = []
        self._claimed: list[float] = []

    @property
    def remaining_seconds(self) -> float:
        return max(0, self._timeout - (self._clock() - self._started))

    @property
    def remaining_calls(self) -> int:
        return self._max_calls - len(self._attempts)

    @property
    def reported_tokens(self) -> int:
        return sum(
            (a.usage.input_tokens or 0) + (a.usage.output_tokens or 0) for a in self._attempts
        )

    def ensure_available(self) -> None:
        self.check_deadline()
        if self.remaining_calls <= 0:
            raise ModelError("generation_call_budget_exceeded")
        if self.reported_tokens >= self._max_tokens:
            raise ModelError("generation_token_budget_exceeded")

    def check_deadline(self) -> None:
        if self.remaining_seconds <= 0:
            raise ModelError("generation_timeout")

    def check_reported_tokens(self) -> None:
        if self.reported_tokens > self._max_tokens:
            raise ModelError("generation_token_budget_exceeded")

    def claim(self, stage: Stage) -> int:
        self.ensure_available()
        if stage not in {"understanding", "answer"}:
            raise ModelError("invalid_generation_stage")
        self._attempts.append(GenerationAttempt(stage))
        self._claimed.append(self._clock())
        return len(self._attempts) - 1

    def record(self, index: int, usage: ModelUsage, model: str) -> None:
        if (
            type(index) is not int
            or not 0 <= index < len(self._attempts)
            or not isinstance(usage, ModelUsage)
            or any(
                v is not None and (type(v) is not int or v < 0)
                for v in (usage.input_tokens, usage.output_tokens)
            )
            or not isinstance(model, str)
            or not 1 <= len(model) <= 160
            or any(not 33 <= ord(c) <= 126 for c in model)
        ):
            raise ModelError("invalid_generation_usage")
        previous = self._attempts[index]
        self._attempts[index] = GenerationAttempt(
            previous.stage, usage, model, round((self._clock() - self._claimed[index]) * 1000)
        )

    def finish(self, index: int) -> None:
        """失败/取消也结束计时；已经取得的用量不能被未知值覆盖。"""
        previous = self._attempts[index]
        if previous.elapsed_ms is None:
            self._attempts[index] = replace(
                previous, elapsed_ms=round((self._clock() - self._claimed[index]) * 1000)
            )

    def usage(self, stage: Stage | None = None) -> dict[str, JsonValue]:
        attempts = [a for a in self._attempts if stage is None or a.stage == stage]
        inputs, outputs = (
            [a.usage.input_tokens for a in attempts],
            [a.usage.output_tokens for a in attempts],
        )
        return {
            "generation_attempts": len(attempts),
            "model_called": bool(attempts),
            "elapsed_ms": sum(a.elapsed_ms or 0 for a in attempts),
            "input_tokens": sum(v for v in inputs if v is not None)
            if inputs and all(v is not None for v in inputs)
            else None,
            "output_tokens": sum(v for v in outputs if v is not None)
            if outputs and all(v is not None for v in outputs)
            else None,
            "model": attempts[-1].model if attempts else None,
            "cost": None,
        }

    def to_json(self) -> dict[str, JsonValue]:
        return {
            **self.usage(),
            "elapsed_ms": round((self._clock() - self._started) * 1000),
            "understanding": self.usage("understanding"),
            "answer": self.usage("answer"),
        }
