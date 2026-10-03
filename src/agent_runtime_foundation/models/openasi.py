"""OpenASI Responses HTTP 接入：一次请求、固定来源、有界正文和总超时。"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from agent_runtime_foundation.application.model_ports import (
    ModelError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from agent_runtime_foundation.domain.task import JsonValue

BASE_URL = "https://api.openasi.bitmiracle.cn/v1"
MODEL = "gpt-5.6-sol"
MAX_RESPONSE_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class OpenASISettings:
    """密钥不进入 repr；Base URL 只允许用户已选定的 HTTPS 来源。"""

    api_key: str = field(repr=False)
    base_url: str = BASE_URL
    model: str = MODEL
    timeout_seconds: float = 60.0
    structured_outputs: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_key, str)
            or not 1 <= len(self.api_key) <= 512
            or any(char.isspace() or not 33 <= ord(char) <= 126 for char in self.api_key)
        ):
            raise ModelError("model_key_not_configured")
        if not isinstance(self.base_url, str):
            raise ModelError("invalid_model_configuration")
        try:
            parsed = urlsplit(self.base_url)
            valid = (
                parsed.scheme == "https"
                and parsed.netloc == "api.openasi.bitmiracle.cn"
                and parsed.path.rstrip("/") in {"/v1", "/v1/responses"}
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if (
            not valid
            or self.model != MODEL
            or isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 1 <= self.timeout_seconds <= 120
            or not isinstance(self.structured_outputs, bool)
        ):
            raise ModelError("invalid_model_configuration")

    @property
    def endpoint(self) -> str:
        """接受 /v1 或完整 /v1/responses，避免重复追加路径。"""
        return BASE_URL + "/responses"

    @classmethod
    def from_env(cls) -> "OpenASISettings":
        """只读取 OpenASI 配置，不向 OpenAI 或其他服务商回退。"""
        try:
            timeout = float(os.getenv("OPENASI_TIMEOUT_SECONDS", "60"))
            mode = os.getenv("OPENASI_STRUCTURED_OUTPUTS", "false")
            if mode not in {"true", "false"}:
                raise ValueError
            return cls(
                os.getenv("OPENASI_API_KEY", ""),
                os.getenv("OPENASI_BASE_URL", BASE_URL),
                os.getenv("OPENASI_MODEL", MODEL),
                timeout,
                mode == "true",
            )
        except ValueError:
            raise ModelError("invalid_model_configuration") from None


def request_payload(request: ModelRequest, settings: OpenASISettings) -> dict[str, JsonValue]:
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


def parse_response(value: object) -> ModelResponse:
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
    if model != MODEL:
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


class OpenASIClient:
    """每次生成创建并关闭 HTTP Client；无自动重试、重定向或代理环境读取。"""

    def __init__(
        self, settings: OpenASISettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """总超时覆盖连接和读流；错误正文不读取，取消原样传播。"""
        payload = request_payload(request, self._settings)
        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=self._settings.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        self._settings.endpoint,
                        headers={"Authorization": "Bearer " + self._settings.api_key},
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
                        try:
                            value: object = json.loads(raw)
                        except (ValueError, UnicodeError):
                            raise ModelError("invalid_model_response") from None
                        return parse_response(value)
        except (TimeoutError, httpx.TimeoutException):
            raise ModelError("model_timeout") from None
        except httpx.HTTPError:
            raise ModelError("model_unavailable") from None


def configured_client() -> OpenASIClient:
    """延迟读取凭据：先取得足够证据才调用此工厂。"""
    return OpenASIClient(OpenASISettings.from_env())
