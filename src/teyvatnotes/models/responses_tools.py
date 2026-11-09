"""通用 Responses 工具轮次：严格续接、显式配置来源和单次有界请求。"""

import json
import re
from typing import cast

import httpx

from teyvatnotes.application.model_ports import ModelError, ModelMessage, ModelUsage
from teyvatnotes.application.tool_model_ports import (
    ToolCall,
    ToolDefinition,
    ToolTurnRequest,
    ToolTurnResponse,
)
from teyvatnotes.domain import JsonValue
from teyvatnotes.models.responses import (
    ResponsesSettings,
    _response_bytes,
    token_count,
)

MAX_REQUEST_BYTES = 48_000
MAX_ARGUMENT_CHARS = 4_000
MAX_OUTPUT_CHARS = 32_000
MAX_HISTORY_ITEMS = 64
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    """JSON 重复键不能靠最后一个覆盖，避免不同实现对角色或调用参数产生歧义。"""
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non_json_constant")


def _encoded(value: object, code: str) -> bytes:
    """只接受有限 JSON；编码后独立复制，避免调用方修改续接对象。"""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise ModelError(code) from None


def _string(
    value: object, maximum: int, code: str, *, pattern: re.Pattern[str] | None = None
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise ModelError(code)
    return value


def _keys(item: dict[str, JsonValue], required: set[str], optional: set[str], code: str) -> None:
    if not required <= item.keys() or not item.keys() <= required | optional:
        raise ModelError(code)


def _optional_metadata(item: dict[str, JsonValue], code: str) -> None:
    if "id" in item:
        _string(item["id"], 160, code, pattern=_IDENTIFIER)
    if "status" in item and item["status"] != "completed":
        raise ModelError(code)


def _normalized_item(value: object, code: str, *, allow_tool_output: bool) -> dict[str, JsonValue]:
    """只接受明确的 Responses 项，不能用 opaque 历史注入 system/user 或内置工具。"""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ModelError(code)
    item = cast(dict[str, JsonValue], value)
    item_type = item.get("type")
    if item_type == "function_call":
        _keys(item, {"type", "call_id", "name", "arguments"}, {"id", "status"}, code)
        _optional_metadata(item, code)
        _string(item["call_id"], 128, code, pattern=_IDENTIFIER)
        _string(item["name"], 64, code, pattern=_NAME)
        # JSON 及业务参数是否合法由工具注册表裁决并记录，不把字符串当执行权限。
        _string(item["arguments"], MAX_ARGUMENT_CHARS, code)
    elif item_type == "function_call_output" and allow_tool_output:
        _keys(item, {"type", "call_id", "output"}, set(), code)
        _string(item["call_id"], 128, code, pattern=_IDENTIFIER)
        _string(item["output"], MAX_OUTPUT_CHARS, code)
    elif item_type == "reasoning":
        _keys(item, {"type", "summary", "encrypted_content"}, {"id", "status"}, code)
        _optional_metadata(item, code)
        _string(item["encrypted_content"], MAX_REQUEST_BYTES, code)
        summary = item["summary"]
        if not isinstance(summary, list) or len(summary) > 20:
            raise ModelError(code)
        for part in summary:
            if not isinstance(part, dict):
                raise ModelError(code)
            _keys(part, {"type", "text"}, set(), code)
            if part["type"] != "summary_text":
                raise ModelError(code)
            _string(part["text"], MAX_OUTPUT_CHARS, code)
    elif item_type == "message":
        _keys(item, {"type", "role", "content"}, {"id", "status"}, code)
        _optional_metadata(item, code)
        if item["role"] != "assistant":
            raise ModelError(code)
        content = item["content"]
        if not isinstance(content, list) or not 1 <= len(content) <= 20:
            raise ModelError(code)
        for part in content:
            if not isinstance(part, dict):
                raise ModelError(code)
            if part.get("type") == "refusal":
                raise ModelError("model_refused" if code == "invalid_model_response" else code)
            _keys(part, {"type", "text"}, {"annotations", "logprobs"}, code)
            if part["type"] != "output_text":
                raise ModelError(code)
            _string(part["text"], MAX_OUTPUT_CHARS, code)
            # 引用只能从资料库派生，不信任服务商附加的链接或输出元数据。
            if part.get("annotations", []) != [] or part.get("logprobs", []) != []:
                raise ModelError(code)
    else:
        raise ModelError(code)
    copied: object = json.loads(_encoded(item, code))
    return cast(dict[str, JsonValue], copied)


def _history(values: object) -> list[JsonValue]:
    """历史调用与结果逐一配对；不允许孤立结果、未完成调用或重复调用 ID。"""
    code = "invalid_model_request"
    if not isinstance(values, tuple) or len(values) > MAX_HISTORY_ITEMS:
        raise ModelError(code)
    result: list[JsonValue] = []
    pending: str | None = None
    seen: set[str] = set()
    for value in values:
        item = _normalized_item(value, code, allow_tool_output=True)
        item_type = item["type"]
        if item_type == "function_call":
            call_id = cast(str, item["call_id"])
            if pending is not None or call_id in seen:
                raise ModelError(code)
            pending = call_id
            seen.add(call_id)
        elif item_type == "function_call_output":
            if pending is None or item["call_id"] != pending:
                raise ModelError(code)
            pending = None
        elif item_type == "message" and pending is not None:
            raise ModelError(code)
        result.append(item)
    if pending is not None:
        raise ModelError(code)
    return result


def _parameters(value: object, code: str) -> dict[str, JsonValue]:
    """严格函数 Schema 的顶层必须关闭额外字段并声明所有属性为 required。"""
    if not isinstance(value, dict):
        raise ModelError(code)
    properties = value.get("properties")
    required = value.get("required")
    if (
        value.get("type") != "object"
        or value.get("additionalProperties") is not False
        or not isinstance(properties, dict)
        or not isinstance(required, list)
        or any(not isinstance(key, str) for key in properties)
        or any(not isinstance(key, str) for key in required)
        or len(required) != len(properties)
        or set(required) != set(properties)
    ):
        raise ModelError(code)
    encoded = _encoded(value, code)
    if len(encoded) > 20_000:
        raise ModelError(code)
    return cast(dict[str, JsonValue], json.loads(encoded))


def tool_request_payload(
    request: ToolTurnRequest, settings: ResponsesSettings
) -> dict[str, JsonValue]:
    """完整保留消息与已校验历史；模型只能串行调用显式函数工具。"""
    code = "invalid_model_request"
    if (
        not isinstance(request, ToolTurnRequest)
        or type(request.max_output_tokens) is not int
        or not 1 <= request.max_output_tokens <= 4096
        or not isinstance(request.messages, tuple)
        or not 1 <= len(request.messages) <= 20
        or not isinstance(request.tools, tuple)
        or not 1 <= len(request.tools) <= 8
    ):
        raise ModelError(code)
    inputs: list[JsonValue] = []
    for message in request.messages:
        if (
            not isinstance(message, ModelMessage)
            or not isinstance(message.role, str)
            or message.role not in {"system", "user", "assistant"}
        ):
            raise ModelError(code)
        content = _string(message.content, MAX_REQUEST_BYTES, code)
        inputs.append({"role": message.role, "content": content})
    inputs.extend(_history(request.history))
    tools: list[JsonValue] = []
    names: set[str] = set()
    for definition in request.tools:
        if not isinstance(definition, ToolDefinition):
            raise ModelError(code)
        name = _string(definition.name, 64, code, pattern=_NAME)
        if name in names:
            raise ModelError(code)
        names.add(name)
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": _string(definition.description, 2000, code),
                "parameters": _parameters(definition.parameters, code),
                "strict": True,
            }
        )
    payload: dict[str, JsonValue] = {
        "model": settings.model,
        "input": inputs,
        "tools": tools,
        "max_output_tokens": request.max_output_tokens,
        "store": False,
        "parallel_tool_calls": False,
        "include": ["reasoning.encrypted_content"],
    }
    # 统计整个请求的 UTF-8 编码，而不是忽略历史/Schema 或只统计中文字符数。
    if len(_encoded(payload, code)) > MAX_REQUEST_BYTES:
        raise ModelError(code)
    return payload


def parse_tool_response(value: object, expected_model: str) -> ToolTurnResponse:
    """一轮只能为一个函数调用或最终正文；任何非白名单输出均不续接。"""
    code = "invalid_model_response"
    if not isinstance(value, dict):
        raise ModelError(code)
    if value.get("status") == "incomplete":
        raise ModelError("model_incomplete")
    if value.get("status") != "completed":
        raise ModelError(code)
    model = _string(value.get("model"), 160, code)
    if model != expected_model:
        raise ModelError("model_identity_mismatch")
    outputs = value.get("output")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 32:
        raise ModelError(code)
    continuation: list[dict[str, JsonValue]] = []
    calls: list[ToolCall] = []
    texts: list[str] = []
    for output in outputs:
        item = _normalized_item(output, code, allow_tool_output=False)
        continuation.append(item)
        if item["type"] == "function_call":
            calls.append(
                ToolCall(
                    cast(str, item["call_id"]),
                    cast(str, item["name"]),
                    cast(str, item["arguments"]),
                )
            )
        elif item["type"] == "message":
            content = cast(list[dict[str, JsonValue]], item["content"])
            texts.extend(cast(str, part["text"]) for part in content)
    text = "".join(texts)
    if len(calls) > 1 or (calls and texts) or (not calls and not text.strip()):
        raise ModelError(code)
    if len(text) > MAX_OUTPUT_CHARS:
        raise ModelError(code)
    # 即使 HTTP 有界，续接状态仍必须有独立预算，不能保存不可再次发送的巨型状态。
    if len(_encoded(continuation, code)) > MAX_REQUEST_BYTES:
        raise ModelError(code)
    usage = value.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return ToolTurnResponse(
        text if texts else None,
        tuple(calls),
        tuple(continuation),
        model,
        ModelUsage(token_count(usage.get("input_tokens")), token_count(usage.get("output_tokens"))),
    )


class ResponsesToolClient:
    """工具协议独立客户端；没有代理环境、自动重试、重定向或其他供应商回退。"""

    def __init__(
        self, settings: ResponsesSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def generate_turn(self, request: ToolTurnRequest) -> ToolTurnResponse:
        """总超时覆盖连接与响应读流；取消合作传播并关闭 HTTP 资源。"""
        payload = tool_request_payload(request, self._settings)
        async with _response_bytes(self._settings, payload, transport=self._transport) as raw:
            try:
                value: object = json.loads(
                    raw,
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
            except (ValueError, UnicodeError, RecursionError):
                raise ModelError("invalid_model_response") from None
            return parse_tool_response(value, self._settings.model)


def configured_tool_client() -> ResponsesToolClient:
    """用例通过初始资料检查后再延迟读取 LLM_* 配置。"""
    return ResponsesToolClient(ResponsesSettings.from_env())
