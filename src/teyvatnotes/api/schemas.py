"""API 请求与响应 Schema。"""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from teyvatnotes.application import CreateTaskCommand
from teyvatnotes.domain import JsonValue, Task, TaskStatus, TaskType
from teyvatnotes.observability import MetricsSnapshot

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


def to_create_command(
    request: TaskCreateRequest,
    *,
    idempotency_key: str | None = None,
) -> CreateTaskCommand:
    """将 HTTP 请求 Schema 显式转换为应用用例输入。"""

    value = request.root
    if isinstance(value, SumNumbersTaskCreate):
        numbers: list[JsonValue] = list(value.payload.numbers)
        return CreateTaskCommand(
            task_type=TaskType.SUM_NUMBERS,
            payload={"numbers": numbers},
            idempotency_key=idempotency_key,
        )
    return CreateTaskCommand(
        task_type=TaskType.WORD_COUNT,
        payload={"text": value.payload.text},
        idempotency_key=idempotency_key,
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
    idempotency_key: str | None
    status: TaskStatus
    result: TaskResultResponse | None
    version: int
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
            idempotency_key=task.idempotency_key,
            status=task.status,
            result=result,
            version=task.version,
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


class HealthResponse(BaseModel):
    """健康检查成功响应。"""

    status: str
    checks: dict[str, str]


class DurationMetricsResponse(BaseModel):
    """任务耗时聚合响应。"""

    count: int
    total_seconds: float
    average_seconds: float
    max_seconds: float


class MetricsResponse(BaseModel):
    """进程内 attempt 指标响应。"""

    started: int
    completed: int
    succeeded: int
    failed: int
    cancelled: int
    success_rate: float
    duration: DurationMetricsResponse
    failure_types: dict[str, int]

    @classmethod
    def from_snapshot(cls, snapshot: MetricsSnapshot) -> "MetricsResponse":
        return cls(
            started=snapshot.started,
            completed=snapshot.completed,
            succeeded=snapshot.succeeded,
            failed=snapshot.failed,
            cancelled=snapshot.cancelled,
            success_rate=snapshot.success_rate,
            duration=DurationMetricsResponse(
                count=snapshot.duration.count,
                total_seconds=snapshot.duration.total_seconds,
                average_seconds=snapshot.duration.average_seconds,
                max_seconds=snapshot.duration.max_seconds,
            ),
            failure_types=dict(snapshot.failure_types),
        )
