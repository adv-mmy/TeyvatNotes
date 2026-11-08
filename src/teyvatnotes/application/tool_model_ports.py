"""工具生成端口：与旧单轮文本端口分离，不依赖具体供应商或 SDK。"""

from dataclasses import dataclass
from typing import Protocol

from teyvatnotes.application.model_ports import ModelMessage, ModelUsage
from teyvatnotes.domain.knowledge import EvidenceWindow
from teyvatnotes.domain.task import JsonValue


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """只读函数的显式名称、用途和 JSON 参数 Schema。"""

    name: str
    description: str
    parameters: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型提出的调用；arguments 仍是不可信 JSON 字符串。"""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolTurnRequest:
    """首轮消息与有界历史；opaque output 只供协议续接，不作为执行权限。"""

    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...]
    history: tuple[dict[str, JsonValue], ...] = ()
    max_output_tokens: int = 2048


@dataclass(frozen=True, slots=True)
class ToolTurnResponse:
    """一轮为函数调用或最终文本；continuation 保留包括 reasoning 的续接项。"""

    text: str | None
    calls: tuple[ToolCall, ...]
    continuation: tuple[dict[str, JsonValue], ...]
    model: str
    usage: ModelUsage


class ToolModelClient(Protocol):
    async def generate_turn(self, request: ToolTurnRequest) -> ToolTurnResponse:
        """仅一次请求，不自动重试；取消传播。"""


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """返回给模型的有界 JSON 和真正回读到的证据；失败不携带内部异常。"""

    output: dict[str, JsonValue]
    evidence: tuple[EvidenceWindow, ...] = ()
    parameters: dict[str, JsonValue] | None = None
    error: str | None = None


class ReadOnlyToolSession(Protocol):
    """每次问答创建独立会话，不能跨请求共享证据能力和玩家条件。"""

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """只返回白名单，不提供任意函数/URL/文件执行。"""

    async def execute(self, call: ToolCall) -> ToolExecution:
        """校验名称、参数和证据能力后执行；取消不转成工具错误。"""
