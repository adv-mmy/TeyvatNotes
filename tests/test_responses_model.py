"""文本 Responses 协议与配置；共用 HTTP 合同见 test_responses_transport。"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from teyvatnotes.application.model_ports import ModelError, ModelMessage, ModelRequest
from teyvatnotes.models import probe as probe_module
from teyvatnotes.models.responses import (
    ResponsesClient,
    ResponsesSettings,
    parse_response,
)

REQUEST = ModelRequest((ModelMessage("user", "你好"),), 100)
BASE_URL = "https://llm.example.test/v1"
MODEL = "example-model-v1"
FAKE_KEY = "test-only-not-a-real-api-key"


def response_body(**changes: object) -> dict[str, object]:
    return {
        "status": "completed",
        "model": MODEL,
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "你"},
                    {"type": "output_text", "text": "好"},
                ],
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 2},
        **changes,
    }


def test_model_is_explicit_and_response_identity_matches_configuration() -> None:
    settings = ResponsesSettings(FAKE_KEY, BASE_URL, "different-model")
    assert settings.model == "different-model"
    with pytest.raises(ModelError, match="model_identity_mismatch"):
        parse_response(response_body(), settings.model)


def test_request_contract_and_raw_response_output_array() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == BASE_URL + "/responses"
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        payload = json.loads(request.content)
        assert payload == {
            "model": MODEL,
            "input": [{"role": "user", "content": "你好"}],
            "max_output_tokens": 100,
            "store": False,
        }
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        client = ResponsesClient(
            ResponsesSettings(FAKE_KEY, BASE_URL, MODEL), transport=httpx.MockTransport(handle)
        )
        result = await client.generate(REQUEST)
        assert result.text == "你好" and result.model == MODEL
        assert result.usage.input_tokens == 10 and result.usage.output_tokens == 2

    asyncio.run(scenario())
    assert FAKE_KEY not in repr(ResponsesSettings(FAKE_KEY, BASE_URL, MODEL))


@pytest.mark.parametrize("suffix", ["", "/", "/responses", "/responses/"])
def test_base_url_never_duplicates_responses(suffix: str) -> None:
    assert ResponsesSettings(FAKE_KEY, BASE_URL + suffix, MODEL).endpoint == BASE_URL + "/responses"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://llm.example.test/v1",
        "https://user@llm.example.test/v1",
        BASE_URL + "?key=secret",
        BASE_URL + "#secret",
        BASE_URL + "/chat/completions",
    ],
)
def test_endpoint_restrictions(base_url: str) -> None:
    with pytest.raises(ModelError, match="invalid_model_configuration"):
        ResponsesSettings(FAKE_KEY, base_url, MODEL)


def test_env_reads_only_explicit_provider_and_rejects_invalid_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_TIMEOUT_SECONDS",
        "LLM_STRUCTURED_OUTPUTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    with pytest.raises(ModelError, match="model_key_not_configured"):
        ResponsesSettings.from_env()
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", BASE_URL)
    monkeypatch.setenv("LLM_MODEL", MODEL)
    assert ResponsesSettings.from_env().model == MODEL
    for name, value in [
        ("LLM_TIMEOUT_SECONDS", "nan"),
        ("LLM_STRUCTURED_OUTPUTS", "yes"),
        ("LLM_MODEL", "model with whitespace"),
    ]:
        with monkeypatch.context() as context:
            context.setenv(name, value)
            with pytest.raises(ModelError, match="invalid_model_configuration"):
                ResponsesSettings.from_env()


@pytest.mark.parametrize(
    "body,code",
    [
        ({"status": "incomplete"}, "model_incomplete"),
        ({"status": "failed"}, "invalid_model_response"),
        (response_body(output=[]), "invalid_model_response"),
        (response_body(model="other"), "model_identity_mismatch"),
        (
            response_body(
                output=[
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": "PRIVATE"}],
                    }
                ]
            ),
            "model_refused",
        ),
        (
            response_body(
                output=[
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": 3}],
                    }
                ]
            ),
            "invalid_model_response",
        ),
    ],
)
def test_response_validation(body: object, code: str) -> None:
    with pytest.raises(ModelError, match=code):
        parse_response(body, MODEL)


def test_unknown_usage_is_none_not_zero() -> None:
    result = parse_response(response_body(usage={"input_tokens": True, "output_tokens": -1}), MODEL)
    assert result.usage.input_tokens is None and result.usage.output_tokens is None


@pytest.mark.parametrize("extra", ['"status":"failed",', '"ignored":NaN,'])
def test_text_json_decoding_policy_is_preserved(extra: str) -> None:
    """共享传输不改变文本路径原有的普通 JSON 解码策略。"""
    raw = ("{" + extra + json.dumps(response_body())[1:]).encode()

    async def scenario() -> None:
        client = ResponsesClient(
            ResponsesSettings(FAKE_KEY, BASE_URL, MODEL),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw)),
        )
        assert (await client.generate(REQUEST)).text == "你好"

    asyncio.run(scenario())


def test_schema_is_explicit_opt_in_no_compatibility_fallback() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["text"]["format"]["type"] == "json_schema"
        return httpx.Response(400)

    async def scenario() -> None:
        client = ResponsesClient(
            ResponsesSettings(FAKE_KEY, BASE_URL, MODEL, structured_outputs=True),
            transport=httpx.MockTransport(handle),
        )
        with pytest.raises(ModelError, match="model_request_rejected"):
            await client.generate(replace(REQUEST, response_schema={"type": "object"}))

    asyncio.run(scenario())


@pytest.mark.parametrize("tokens", [True, 0, 4097])
def test_invalid_request_has_no_http_call(tokens: int) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("非法请求不发送")

    async def scenario() -> None:
        client = ResponsesClient(
            ResponsesSettings(FAKE_KEY, BASE_URL, MODEL), transport=httpx.MockTransport(handle)
        )
        with pytest.raises(ModelError, match="invalid_model_request"):
            await client.generate(replace(REQUEST, max_output_tokens=tokens))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"messages": (object(),)},
        {"response_schema": {"x": object()}},
        {"response_schema": {"x": float("nan")}},
    ],
)
def test_direct_request_runtime_types_are_stable(changes: dict[str, object]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("非法请求不得发送")

    async def scenario() -> None:
        client = ResponsesClient(
            ResponsesSettings(FAKE_KEY, BASE_URL, MODEL), transport=httpx.MockTransport(handle)
        )
        with pytest.raises(ModelError, match="invalid_model_request"):
            await client.generate(replace(REQUEST, **changes))

    asyncio.run(scenario())


def test_probe_prints_only_result_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["input"] == [{"role": "user", "content": "你好"}]
        assert body["max_output_tokens"] == 128
        return httpx.Response(200, json=response_body())

    monkeypatch.setattr(
        probe_module,
        "configured_client",
        lambda: ResponsesClient(
            ResponsesSettings(FAKE_KEY, BASE_URL, MODEL), transport=httpx.MockTransport(handle)
        ),
    )
    assert probe_module.main() == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "success" and result["model"] == MODEL
    assert "text" not in result and FAKE_KEY not in output


def test_probe_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable() -> ResponsesClient:
        raise ModelError("model_key_not_configured")

    monkeypatch.setattr(probe_module, "configured_client", unavailable)
    assert probe_module.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "model_key_not_configured",
    }
