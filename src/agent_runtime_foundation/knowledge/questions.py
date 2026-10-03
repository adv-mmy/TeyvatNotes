"""问答准备 JSON 的文件/Schema 边界，不读取模型配置或玩家持久会话。"""

import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime_foundation.application.question_preparation import QuestionRequest
from agent_runtime_foundation.domain.guides import GuideImportError, GuideKind

MAX_QUESTION_BYTES = 16_384
ShortText = Annotated[str, Field(min_length=1, max_length=80, pattern=r"\S", strict=True)]
ConditionValue = Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S", strict=True)]


class QuestionSchema(BaseModel):
    """可省略条件以得到追问，但禁止未知字段、空白和无界输入。"""

    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000, pattern=r"\S", strict=True)
    guide_kind: GuideKind | None = None
    game_version: ShortText | None = None
    entity: ShortText | None = None
    keywords: list[ShortText] = Field(default_factory=list, max_length=12)
    conditions: dict[ShortText, ConditionValue] = Field(default_factory=dict, max_length=20)
    goal: ShortText | None = None
    location: ShortText | None = None
    progress: ConditionValue | None = None
    owned_equipment: list[ShortText] | None = Field(default=None, max_length=100)
    owned_characters: list[ShortText] | None = Field(default=None, max_length=100)

    def to_request(self) -> QuestionRequest:
        """显式转换，不将 Pydantic 模型或可变字典泄漏进用例。"""
        return QuestionRequest(
            self.question,
            self.guide_kind,
            self.game_version,
            self.entity,
            tuple(self.keywords),
            tuple(sorted(self.conditions.items())),
            self.goal,
            self.location,
            self.progress,
            tuple(self.owned_equipment) if self.owned_equipment is not None else None,
            tuple(self.owned_characters) if self.owned_characters is not None else None,
        )


def parse_question(value: object) -> QuestionRequest:
    """稳定公开错误中不携带玩家数据、路径或 Pydantic 校验原文。"""
    try:
        return QuestionSchema.model_validate(value).to_request()
    except ValidationError as exc:
        raise GuideImportError("invalid_question") from exc


def load_question(path: Path) -> QuestionRequest:
    """有界 UTF-8/BOM 读取，在线程池中调用，不输出原始内容。"""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_QUESTION_BYTES + 1)
        if len(raw) > MAX_QUESTION_BYTES:
            raise GuideImportError("question_too_large")
        value: object = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise GuideImportError("question_unreadable") from exc
    return parse_question(value)
