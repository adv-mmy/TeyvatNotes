"""服务商无关的异步模型端口、用量与稳定错误。"""

from dataclasses import dataclass
from typing import Literal, Protocol

from teyvatnotes.domain.task import JsonValue


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """模型消息；适配器仍须校验运行时边界。"""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """有界生成请求；response_schema 是否发送由兼容模式显式控制。"""

    messages: tuple[ModelMessage, ...]
    max_output_tokens: int
    response_schema: dict[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """未获得用量时使用 None，不能把未知当成零消耗。"""

    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """模型文本、服务端报告的模型标识与用量，不表示事实已验证。"""

    text: str
    model: str
    usage: ModelUsage


class ModelError(Exception):
    """不泄漏请求/响应正文、凭据或底层异常的公开错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ModelClient(Protocol):
    """提供异步生成能力的结构化类型协议，不依赖某个 SDK。"""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """一次生成；取消合作传播，不承诺外部计费恰好一次。"""
