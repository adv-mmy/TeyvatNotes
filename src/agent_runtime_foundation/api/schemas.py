"""API 请求与响应 Schema。"""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from agent_runtime_foundation.application import CreateTaskCommand
from agent_runtime_foundation.domain import JsonValue, Task, TaskStatus, TaskType

BoundedInteger = Annotated[
    int,
    Field(strict=True, ge=-1_000_000, le=1_000_000),
]


class SumNumbersPayload(BaseModel):
    """数字求和任务参数。"""

    model_config = ConfigDict(extra="forbid")

    numbers: list[BoundedInteger] = Field(min_length=1, max_length=100)


class WordCountPayload(BaseModel):
    """字数统计任务参数。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=10_000)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text 不能只包含空白字符")
        return value


class SumNumbersTaskCreate(BaseModel):
    """创建数字求和任务。"""

    model_config = ConfigDict(extra="forbid")

    task_type: Literal["sum_numbers"]
    payload: SumNumbersPayload


class WordCountTaskCreate(BaseModel):
    """创建字数统计任务。"""

    model_config = ConfigDict(extra="forbid")

    task_type: Literal["word_count"]
    payload: WordCountPayload


TaskCreateVariant = Annotated[
    SumNumbersTaskCreate | WordCountTaskCreate,
    Field(discriminator="task_type"),
]


class TaskCreateRequest(RootModel[TaskCreateVariant]):
    """使用 task_type 区分的创建任务请求。"""


def to_create_command(request: TaskCreateRequest) -> CreateTaskCommand:
    """将 HTTP 请求 Schema 显式转换为应用用例输入。"""

    value = request.root
    if isinstance(value, SumNumbersTaskCreate):
        numbers: list[JsonValue] = list(value.payload.numbers)
        return CreateTaskCommand(
            task_type=TaskType.SUM_NUMBERS,
            payload={"numbers": numbers},
        )
    return CreateTaskCommand(
        task_type=TaskType.WORD_COUNT,
        payload={"text": value.payload.text},
    )


class TaskResultResponse(BaseModel):
    """任务结果响应。"""

    output: JsonValue = None
    error: str | None = None


class TaskResponse(BaseModel):
    """任务响应。"""

    id: UUID
    task_type: TaskType
    payload: dict[str, JsonValue]
    status: TaskStatus
    result: TaskResultResponse | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_task(cls, task: Task) -> Self:
        result = None
        if task.result is not None:
            result = TaskResultResponse(
                output=task.result.output,
                error=task.result.error,
            )
        return cls(
            id=task.id,
            task_type=task.task_type,
            payload=dict(task.payload),
            status=task.status,
            result=result,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class TaskListResponse(BaseModel):
    """分页任务列表。"""

    items: list[TaskResponse]
    total: int
    offset: int
    limit: int


class ErrorResponse(BaseModel):
    """所有 API 错误的统一响应。"""

    code: str
    message: str
    details: JsonValue = Field(default_factory=dict)
    request_id: str
