"""每次问答独立的有界元数据事件；不是公开SSE或模型文本流。"""

import re
from dataclasses import dataclass
from typing import Literal

from teyvatnotes.application.model_ports import ModelError

EventKind = Literal[
    "answer_started",
    "model_started",
    "model_finished",
    "tool_started",
    "tool_finished",
    "answer_finished",
    "answer_cancelled",
]


@dataclass(frozen=True, slots=True)
class AnswerEvent:
    """不包含玩家条件、正文、工具参数、模型reasoning或凭据。"""

    sequence: int
    kind: EventKind
    index: int | None = None
    status: str | None = None
    error: str | None = None


class AnswerEventRecorder:
    """单问答单次使用；只能有一个起点与终点，最多32个事件。"""

    def __init__(self) -> None:
        self._events: list[AnswerEvent] = []
        self._closed = False
        self._active: tuple[str, int] | None = None
        self._models = 0
        self._tools = 0

    @property
    def events(self) -> tuple[AnswerEvent, ...]:
        return tuple(self._events)

    def start(self) -> None:
        if self._events:
            raise ModelError("answer_event_session_reused")
        self.record("answer_started")

    def record(
        self,
        kind: EventKind,
        *,
        index: int | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        if self._closed or len(self._events) >= 32:
            raise ModelError("answer_event_budget_exceeded")
        if (
            not isinstance(kind, str)
            or kind
            not in {
                "answer_started",
                "model_started",
                "model_finished",
                "tool_started",
                "tool_finished",
                "answer_finished",
                "answer_cancelled",
            }
            or (not self._events and kind != "answer_started")
            or (self._events and kind == "answer_started")
            or (index is not None and (type(index) is not int or not 1 <= index <= 4))
            or (
                status is not None
                and (
                    not isinstance(status, str)
                    or status
                    not in {"answer", "needs_clarification", "insufficient_evidence", "error"}
                )
            )
        ):
            raise ModelError("invalid_answer_event")
        if kind in {"model_started", "tool_started"}:
            operation = "model" if kind == "model_started" else "tool"
            count = self._models if operation == "model" else self._tools
            maximum = 4 if operation == "model" else 3
            if (
                self._active is not None
                or index != count + 1
                or count >= maximum
                or status is not None
            ):
                raise ModelError("invalid_answer_event")
            self._active = (operation, count + 1)
            if operation == "model":
                self._models += 1
            else:
                self._tools += 1
        elif kind in {"model_finished", "tool_finished"}:
            operation = "model" if kind == "model_finished" else "tool"
            if self._active != (operation, index) or status is not None:
                raise ModelError("invalid_answer_event")
            self._active = None
        elif (
            self._active is not None
            or index is not None
            or ((kind == "answer_finished") != (status is not None))
        ):
            raise ModelError("invalid_answer_event")
        safe_error = (
            error if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,64}", error) else None
        )
        self._events.append(AnswerEvent(len(self._events) + 1, kind, index, status, safe_error))
        self._closed = kind in {"answer_finished", "answer_cancelled"}
