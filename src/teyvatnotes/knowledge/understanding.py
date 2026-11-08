"""提取模型的严格JSON边界；程序还须验证片段和候选，不信任schema声明。"""

import json
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from teyvatnotes.application.model_ports import ModelError
from teyvatnotes.application.question_understanding import (
    ExtractionPlan,
    ExtractionSegment,
)
from teyvatnotes.domain.task import JsonValue

Offset = Annotated[int, Field(strict=True, ge=0, le=1000)]
Scalar = Annotated[str, Field(strict=True, min_length=1, max_length=160)]
Items = Annotated[list[Scalar], Field(max_length=100)]


class UpdateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field: Literal[
        "guide_kind",
        "entity",
        "game_version",
        "goal",
        "keywords",
        "location",
        "progress",
        "owned_characters",
        "owned_equipment",
    ]
    value: Scalar | Items | None


class SegmentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start: Offset
    end: Offset
    quote: str = Field(min_length=1, max_length=1000)
    updates: list[UpdateSchema] = Field(min_length=1, max_length=9)


class ExtractionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["extracted", "needs_clarification", "unsupported"]
    segments: list[SegmentSchema] = Field(max_length=20)


def _unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError


class JsonUnderstandingDecoder:
    """先拒绝重复键和NaN，再做严格类型校验；不接受Markdown或额外字段。"""

    def schema(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], ExtractionSchema.model_json_schema())

    def decode(self, text: str) -> ExtractionPlan:
        try:
            if not isinstance(text, str) or len(text.encode("utf-8")) > 16_384:
                raise ValueError
            raw = json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
            value = ExtractionSchema.model_validate(raw)
            if any(len({u.field for u in s.updates}) != len(s.updates) for s in value.segments):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError, ValidationError):
            raise ModelError("invalid_understanding_output") from None
        return ExtractionPlan(
            value.status,
            tuple(
                ExtractionSegment(
                    s.start, s.end, s.quote, {u.field: cast(JsonValue, u.value) for u in s.updates}
                )
                for s in value.segments
            ),
        )
