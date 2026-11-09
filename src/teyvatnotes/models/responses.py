"""服务商无关的 Responses HTTP 接入：显式配置、一次请求和有界响应。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from teyvatnotes.application.model_ports import (
    ModelError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from teyvatnotes.domain.json_types import JsonValue

MAX_RESPONSE_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class ResponsesSettings:
    """必须显式提供平台地址、模型与密钥；密钥不进入 repr。"""

    api_key: str = field(repr=False)
    base_url: str
    model: str
    timeout_seconds: float = 60.0
    structured_outputs: bool = False
    api_style: str = "responses"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_key, str)
            or not 1 <= len(self.api_key) <= 512
            or any(char.isspace() or not 33 <= ord(char) <= 126 for char in self.api_key)
        ):
            raise ModelError("model_key_not_configured")
        if (
            not isinstance(self.base_url, str)
            or not self.base_url
            or any(char.isspace() or not 33 <= ord(char) <= 126 for char in self.base_url)
            or any(char in self.base_url for char in ("\\", "?", "#"))
        ):
            raise ModelError("invalid_model_configuration")
        try:
            parsed = urlsplit(self.base_url)
            normalized = httpx.URL(self.base_url)
            valid = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and bool(normalized.host)
                and parsed.username is None
                and parsed.password is None
                and (parsed.port is None or 1 <= parsed.port <= 65535)
                and not parsed.path.rstrip("/").endswith(("/chat/completions", "/messages"))
                and not parsed.query
                and not parsed.fragment
            )
        except (ValueError, httpx.InvalidURL):
            valid = False
        if (
            not valid
            or not isinstance(self.model, str)
            or not 1 <= len(self.model) <= 160
            or any(char.isspace() or not 33 <= ord(char) <= 126 for char in self.model)
            or self.api_style != "responses"
            or isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 1 <= self.timeout_seconds <= 120
            or not isinstance(self.structured_outputs, bool)
        ):
            raise ModelError("invalid_model_configuration")

    @property
    def endpoint(self) -> str:
        """接受 API 基址或完整 /responses 地址，保留配置的域名和路径。"""
        base = self.base_url.rstrip("/")
        return base if base.endswith("/responses") else base + "/responses"

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "ResponsesSettings":
        """只读取 LLM_*；没有厂商默认值、旧变量别名或凭据回退。"""
        env = os.environ if values is None else values
        try:
            timeout = float(env.get("LLM_TIMEOUT_SECONDS", "60"))
            mode = env.get("LLM_STRUCTURED_OUTPUTS", "false")
            if mode not in {"true", "false"}:
                raise ValueError
            return cls(
                env.get("LLM_API_KEY", ""),
                env.get("LLM_BASE_URL", ""),
                env.get("LLM_MODEL", ""),
                timeout,
                mode == "true",
                env.get("LLM_API_STYLE", "responses"),
            )
        except ValueError:
            raise ModelError("invalid_model_configuration") from None


def request_payload(request: ModelRequest, settings: ResponsesSettings) -> dict[str, JsonValue]:
    """默认用提示词要求 JSON；结构化输出仅在用户显式开启时发送。"""
    if (
        not isinstance(request.max_output_tokens, int)
        or isinstance(request.max_output_tokens, bool)
        or not 1 <= request.max_output_tokens <= 4096
        or not isinstance(request.messages, tuple)
        or not 1 <= len(request.messages) <= 20
        or any(
            not isinstance(message, ModelMessage)
            or not isinstance(message.role, str)
            or message.role not in {"system", "user", "assistant"}
            or not isinstance(message.content, str)
            or not message.content.strip()
            for message in request.messages
        )
        or sum(len(message.content) for message in request.messages) > 48_000
    ):
        raise ModelError("invalid_model_request")
    if request.response_schema is not None:
        try:
            if (
                not isinstance(request.response_schema, dict)
                or len(json.dumps(request.response_schema, allow_nan=False)) > 20_000
            ):
                raise ValueError
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise ModelError("invalid_model_request") from None
    payload: dict[str, JsonValue] = {
        "model": settings.model,
        "input": [
            {"role": message.role, "content": message.content} for message in request.messages
        ],
        "max_output_tokens": request.max_output_tokens,
        "store": False,
    }
    if settings.structured_outputs and request.response_schema is not None:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": "guide_answer",
                "strict": True,
                "schema": request.response_schema,
            }
        }
    return payload


def token_count(value: object) -> int | None:
    """用量未知或非法时返回 None，不把 bool、负数或浮点数当 Token。"""
    return value if type(value) is int and value >= 0 else None


def parse_response(value: object, expected_model: str) -> ModelResponse:
    """遍历 assistant 消息的 output_text；拒绝非终态、拒答和缺失正文。"""
    if not isinstance(value, dict):
        raise ModelError("invalid_model_response")
    status = value.get("status")
    if status == "incomplete":
        raise ModelError("model_incomplete")
    if status != "completed":
        raise ModelError("invalid_model_response")
    model = value.get("model")
    outputs = value.get("output")
    if not isinstance(model, str) or not model.strip() or len(model) > 160:
        raise ModelError("invalid_model_response")
    if model != expected_model:
        raise ModelError("model_identity_mismatch")
    if not isinstance(outputs, list):
        raise ModelError("invalid_model_response")
    texts: list[str] = []
    for output in outputs:
        if not isinstance(output, dict):
            raise ModelError("invalid_model_response")
        if output.get("type") != "message":
            continue
        if output.get("role") != "assistant" or output.get("status", "completed") != "completed":
            raise ModelError("invalid_model_response")
        content = output.get("content")
        if not isinstance(content, list):
            raise ModelError("invalid_model_response")
        for part in content:
            if not isinstance(part, dict):
                raise ModelError("invalid_model_response")
            if part.get("type") == "refusal":
                raise ModelError("model_refused")
            if part.get("type") == "output_text":
                text = part.get("text")
                if not isinstance(text, str):
                    raise ModelError("invalid_model_response")
                texts.append(text)
    text = "".join(texts)
    if not text.strip() or len(text) > 32_000:
        raise ModelError("invalid_model_response")
    usage = value.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return ModelResponse(
        text,
        model,
        ModelUsage(token_count(usage.get("input_tokens")), token_count(usage.get("output_tokens"))),
    )


@asynccontextmanager
async def _response_bytes(
    settings: ResponsesSettings,
    payload: dict[str, JsonValue],
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> AsyncIterator[bytearray]:
    """集中传输合同；调用方的协议解析与资源清理仍处于总超时和错误映射内。"""
    try:
        async with asyncio.timeout(settings.timeout_seconds):
            async with httpx.AsyncClient(
                transport=transport,
                timeout=settings.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    settings.endpoint,
                    headers={"Authorization": "Bearer " + settings.api_key},
                    json=payload,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise ModelError("model_authentication_failed")
                    if response.status_code == 429:
                        raise ModelError("model_rate_limited")
                    if response.status_code >= 500:
                        raise ModelError("model_unavailable")
                    if response.status_code != 200:
                        raise ModelError("model_request_rejected")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ModelError("model_response_too_large")
                        raw.extend(chunk)
                    # yield 保留在三个上下文内部，解析失败和取消也按原顺序关闭资源。
                    yield raw
    except (TimeoutError, httpx.TimeoutException):
        raise ModelError("model_timeout") from None
    except httpx.HTTPError:
        raise ModelError("model_unavailable") from None


class ResponsesClient:
    """每次生成创建并关闭 HTTP Client；无自动重试、重定向或代理环境读取。"""

    def __init__(
        self, settings: ResponsesSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """总超时覆盖连接和读流；错误正文不读取，取消原样传播。"""
        payload = request_payload(request, self._settings)
        async with _response_bytes(self._settings, payload, transport=self._transport) as raw:
            try:
                value: object = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ModelError("invalid_model_response") from None
            return parse_response(value, self._settings.model)


def configured_client() -> ResponsesClient:
    """延迟读取凭据：先取得足够证据才调用此工厂。"""
    return ResponsesClient(ResponsesSettings.from_env())
