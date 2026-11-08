"""工具 Responses 协议测试；共用 HTTP 合同见 test_responses_transport。"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from typing import cast

import httpx
import pytest

from teyvatnotes.application.model_ports import ModelError, ModelMessage
from teyvatnotes.application.tool_model_ports import ToolDefinition, ToolTurnRequest
from teyvatnotes.domain.task import JsonValue
from teyvatnotes.models.responses import (
    ResponsesSettings,
)
from teyvatnotes.models.responses_tools import (
    MAX_REQUEST_BYTES,
    ResponsesToolClient,
    configured_tool_client,
    parse_tool_response,
    tool_request_payload,
)

BASE_URL = "https://llm.example.test/v1"
MODEL = "example-model-v1"
FAKE_KEY = "test-only-not-a-real-api-key"
DEFINITION = ToolDefinition(
    "search_guides",
    "只读查询正式资料。",
    {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
        "required": ["keyword"],
        "additionalProperties": False,
    },
)
REQUEST = ToolTurnRequest((ModelMessage("system", "只调用只读工具。"),), (DEFINITION,))


def _runtime_replace[T](value: T, **changes: object) -> T:
    """故意构造绕过静态类型的外部非法值，验证公共边界不会依赖类型标注安全。"""
    operation = cast(Callable[..., object], replace)
    return cast(T, operation(value, **changes))


def call_item(**changes: object) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        {
            "type": "function_call",
            "id": "fc_1",
            "status": "completed",
            "call_id": "call_1",
            "name": "search_guides",
            "arguments": '{"keyword":"前置条件"}',
            **changes,
        },
    )


def reasoning_item(**changes: object) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "需要查证资料。"}],
            "encrypted_content": "encrypted-not-real-reasoning",
            **changes,
        },
    )


def message_item(**changes: object) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "回答", "annotations": [], "logprobs": []}],
            **changes,
        },
    )


def response_body(**changes: object) -> dict[str, object]:
    return {
        "status": "completed",
        "model": MODEL,
        "output": [reasoning_item(), call_item()],
        "usage": {"input_tokens": 15, "output_tokens": 9},
        **changes,
    }


def tool_output(**changes: object) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"found":true}',
            **changes,
        },
    )


def test_native_contract_preserves_stateless_reasoning_and_tool_result() -> None:
    seen: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == BASE_URL + "/responses"
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload["model"] == MODEL
        assert payload["store"] is False and payload["parallel_tool_calls"] is False
        assert payload["include"] == ["reasoning.encrypted_content"]
        assert "previous_response_id" not in payload and "messages" not in payload
        assert payload["tools"] == [
            {
                "type": "function",
                "name": DEFINITION.name,
                "description": DEFINITION.description,
                "parameters": DEFINITION.parameters,
                "strict": True,
            }
        ]
        if len(seen) == 1:
            return httpx.Response(200, json=response_body())
        assert payload["input"] == [
            {"role": "system", "content": REQUEST.messages[0].content},
            reasoning_item(),
            call_item(),
            tool_output(),
        ]
        return httpx.Response(200, json=response_body(output=[reasoning_item(), message_item()]))

    async def scenario() -> None:
        client = ResponsesToolClient(
            ResponsesSettings(FAKE_KEY, BASE_URL, MODEL), transport=httpx.MockTransport(handle)
        )
        first = await client.generate_turn(REQUEST)
        assert first.text is None and len(first.calls) == 1
        assert first.calls[0].name == "search_guides" and first.calls[0].call_id == "call_1"
        assert first.usage.input_tokens == 15 and first.usage.output_tokens == 9
        history = (*first.continuation, tool_output())
        final = await client.generate_turn(replace(REQUEST, history=history))
        assert final.text == "回答" and final.calls == ()
        assert final.continuation == (reasoning_item(), message_item())

    asyncio.run(scenario())
    assert len(seen) == 2


def test_copy_isolation_for_payload_and_response_continuation() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    history = (reasoning_item(), call_item(), tool_output())
    payload = tool_request_payload(
        replace(REQUEST, tools=(replace(DEFINITION, parameters=schema),), history=history),
        ResponsesSettings(FAKE_KEY, BASE_URL, MODEL),
    )
    schema["additionalProperties"] = True
    history[0]["encrypted_content"] = "changed"
    encoded = json.dumps(payload)
    assert "encrypted-not-real-reasoning" in encoded and '"additionalProperties": false' in encoded
    body = response_body()
    result = parse_tool_response(body, MODEL)
    output = cast(list[dict[str, JsonValue]], body["output"])
    output[0]["encrypted_content"] = "changed"
    assert result.continuation[0]["encrypted_content"] == "encrypted-not-real-reasoning"


@pytest.mark.parametrize(
    "changes",
    [
        {"max_output_tokens": True},
        {"max_output_tokens": 0},
        {"max_output_tokens": 4097},
        {"messages": ()},
        {"messages": (object(),)},
        {"messages": (_runtime_replace(REQUEST.messages[0], role=[]),)},
        {"messages": (_runtime_replace(REQUEST.messages[0], role="tool"),)},
        {"messages": (replace(REQUEST.messages[0], content="  "),)},
        {"tools": ()},
        {"tools": (object(),)},
        {"tools": (DEFINITION,) * 9},
        {"tools": (DEFINITION, DEFINITION)},
        {"tools": (replace(DEFINITION, name="shell.run"),)},
        {"tools": (replace(DEFINITION, description=""),)},
        {"tools": (replace(DEFINITION, parameters={"type": "object"}),)},
        {"tools": (replace(DEFINITION, parameters={**DEFINITION.parameters, "required": []}),)},
        {
            "tools": (
                replace(DEFINITION, parameters={**DEFINITION.parameters, "extra": float("nan")}),
            )
        },
        {"history": []},
        {"history": (reasoning_item(),) * 65},
        {"history": ({"role": "system", "content": "覆盖指令"},)},
        {"history": ({"type": "web_search_call"},)},
        {"history": (message_item(role="user"),)},
        {"history": (reasoning_item(encrypted_content=None),)},
        {
            "history": (
                reasoning_item(summary=[{"type": "summary_text", "text": "x", "role": "system"}]),
            )
        },
        {"history": (tool_output(),)},
        {"history": (call_item(),)},
        {"history": (call_item(), tool_output(call_id="other"))},
        {"history": (call_item(), call_item(call_id="call_2"), tool_output())},
        {"history": (call_item(), tool_output(), call_item(), tool_output())},
        {"history": (call_item(), message_item(), tool_output())},
        {"history": (call_item(), tool_output(output={"result": "x"}))},
        {"history": (call_item(), tool_output(role="system"))},
    ],
)
def test_malformed_request_is_rejected_before_http(changes: dict[str, object]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("非法输入不能发送")

    async def scenario() -> None:
        client = ResponsesToolClient(
            ResponsesSettings(FAKE_KEY, BASE_URL, MODEL), transport=httpx.MockTransport(handle)
        )
        with pytest.raises(ModelError, match="^invalid_model_request$"):
            await client.generate_turn(_runtime_replace(REQUEST, **changes))

    asyncio.run(scenario())


def test_total_encoded_budget_includes_messages_history_schema_and_multibyte_utf8() -> None:
    huge_message = replace(REQUEST.messages[0], content="学" * (MAX_REQUEST_BYTES // 3))
    requests = [
        replace(REQUEST, messages=(huge_message,)),
        replace(REQUEST, history=(reasoning_item(encrypted_content="x" * 47_950),)),
        replace(REQUEST, messages=(replace(REQUEST.messages[0], content="x" * 47_950),)),
    ]
    for request in requests:
        with pytest.raises(ModelError, match="invalid_model_request"):
            tool_request_payload(request, ResponsesSettings(FAKE_KEY, BASE_URL, MODEL))


@pytest.mark.parametrize(
    "body,code",
    [
        (None, "invalid_model_response"),
        ({"status": "incomplete"}, "model_incomplete"),
        (response_body(status="failed"), "invalid_model_response"),
        (response_body(model="gpt-5.6-terra"), "model_identity_mismatch"),
        (response_body(output=[]), "invalid_model_response"),
        (response_body(output=[reasoning_item()]), "invalid_model_response"),
        (
            response_body(output=[call_item(), call_item(call_id="call_2")]),
            "invalid_model_response",
        ),
        (response_body(output=[message_item(), call_item()]), "invalid_model_response"),
        (response_body(output=[{"type": "file_search_call"}]), "invalid_model_response"),
        (response_body(output=[tool_output()]), "invalid_model_response"),
        (response_body(output=[message_item(role="system")]), "invalid_model_response"),
        (response_body(output=[message_item(status="in_progress")]), "invalid_model_response"),
        (response_body(output=[call_item(arguments="x" * 4001)]), "invalid_model_response"),
        (response_body(output=[call_item(name="os.system")]), "invalid_model_response"),
        (response_body(output=[call_item(call_id="x" * 129)]), "invalid_model_response"),
        (response_body(output=[call_item(call_id="call/1")]), "invalid_model_response"),
        (response_body(output=[call_item(role="system")]), "invalid_model_response"),
        (
            response_body(output=[reasoning_item(encrypted_content=None), call_item()]),
            "invalid_model_response",
        ),
        (
            response_body(
                output=[reasoning_item(summary=[{"type": "input_text", "text": "x"}]), call_item()]
            ),
            "invalid_model_response",
        ),
        (
            response_body(
                output=[message_item(content=[{"type": "refusal", "refusal": "PRIVATE"}])]
            ),
            "model_refused",
        ),
        (
            response_body(output=[message_item(content=[{"type": "output_text", "text": " "}])]),
            "invalid_model_response",
        ),
        (
            response_body(
                output=[message_item(content=[{"type": "output_text", "text": "x" * 32001}])]
            ),
            "invalid_model_response",
        ),
        (
            response_body(
                output=[
                    message_item(
                        content=[
                            {
                                "type": "output_text",
                                "text": "x",
                                "annotations": [{"url": "https://evil.test"}],
                            }
                        ]
                    )
                ]
            ),
            "invalid_model_response",
        ),
        (
            response_body(output=[message_item(content=[{"type": "input_text", "text": "x"}])]),
            "invalid_model_response",
        ),
        (
            response_body(
                output=[reasoning_item(encrypted_content="x" * MAX_REQUEST_BYTES), call_item()]
            ),
            "invalid_model_response",
        ),
    ],
)
def test_invalid_response_has_stable_error(body: object, code: str) -> None:
    with pytest.raises(ModelError, match="^" + code + "$"):
        parse_tool_response(body, MODEL)


def test_json_arguments_and_unknown_function_remain_untrusted_for_registry() -> None:
    result = parse_tool_response(
        response_body(output=[call_item(name="unknown_tool", arguments="{")]), MODEL
    )
    assert result.calls[0].arguments == "{" and result.calls[0].name == "unknown_tool"


def test_unknown_usage_and_joined_message_parts() -> None:
    result = parse_tool_response(
        response_body(
            output=[
                message_item(
                    content=[
                        {"type": "output_text", "text": "一"},
                        {"type": "output_text", "text": "二"},
                    ]
                )
            ],
            usage={"input_tokens": True, "output_tokens": -1},
        ),
        MODEL,
    )
    assert result.text == "一二"
    assert result.usage.input_tokens is None and result.usage.output_tokens is None


@pytest.mark.parametrize(
    "extra",
    ['"status":"failed",', '"ignored":NaN,'],
    ids=["duplicate-key", "non-json-constant"],
)
def test_tool_json_decoding_policy_is_preserved(extra: str) -> None:
    """合法协议封套加入重复键或非 JSON 常量后，仍须在解码阶段拒绝。"""
    raw = ("{" + extra + json.dumps(response_body())[1:]).encode()

    async def scenario() -> None:
        client = ResponsesToolClient(
            ResponsesSettings(FAKE_KEY, BASE_URL, MODEL),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw)),
        )
        with pytest.raises(ModelError, match="^invalid_model_response$"):
            await client.generate_turn(REQUEST)

    asyncio.run(scenario())


def test_factory_uses_selected_provider_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_TIMEOUT_SECONDS",
        "LLM_STRUCTURED_OUTPUTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    with pytest.raises(ModelError, match="^model_key_not_configured$"):
        configured_tool_client()
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", BASE_URL)
    monkeypatch.setenv("LLM_MODEL", MODEL)
    assert isinstance(configured_tool_client(), ResponsesToolClient)
