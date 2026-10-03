"""OpenASI HTTP 契约、正文上限、失败脱敏和取消；全部使用 MockTransport。"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from agent_runtime_foundation.application.model_ports import ModelError, ModelMessage, ModelRequest
from agent_runtime_foundation.models import probe as probe_module
from agent_runtime_foundation.models.openasi import (
    BASE_URL,
    MAX_RESPONSE_BYTES,
    MODEL,
    OpenASIClient,
    OpenASISettings,
    parse_response,
)

REQUEST = ModelRequest((ModelMessage("user", "你好"),), 100)
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


def test_selected_model_is_sol_and_terra_is_not_a_fallback() -> None:
    """明确用户选型，防止请求与测试一起意外保留旧常量而自洽通过。"""
    assert MODEL == "gpt-5.6-sol"
    assert OpenASISettings(FAKE_KEY).model == "gpt-5.6-sol"
    with pytest.raises(ModelError, match="invalid_model_configuration"):
        OpenASISettings(FAKE_KEY, model="gpt-5.6-terra")
    with pytest.raises(ModelError, match="model_identity_mismatch"):
        parse_response(response_body(model="gpt-5.6-terra"))


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
        client = OpenASIClient(OpenASISettings(FAKE_KEY), transport=httpx.MockTransport(handle))
        result = await client.generate(REQUEST)
        assert result.text == "你好" and result.model == MODEL
        assert result.usage.input_tokens == 10 and result.usage.output_tokens == 2

    asyncio.run(scenario())
    assert FAKE_KEY not in repr(OpenASISettings(FAKE_KEY))


@pytest.mark.parametrize("suffix", ["", "/", "/responses", "/responses/"])
def test_base_url_never_duplicates_responses(suffix: str) -> None:
    assert OpenASISettings(FAKE_KEY, BASE_URL + suffix).endpoint == BASE_URL + "/responses"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.openasi.bitmiracle.cn/v1",
        "https://evil.test/v1",
        "https://api.openasi.bitmiracle.cn.evil.test/v1",
        "https://user@api.openasi.bitmiracle.cn/v1",
        BASE_URL + "?key=secret",
        BASE_URL + "#secret",
        BASE_URL + "/chat/completions",
    ],
)
def test_endpoint_restrictions(base_url: str) -> None:
    with pytest.raises(ModelError, match="invalid_model_configuration"):
        OpenASISettings(FAKE_KEY, base_url)


def test_env_reads_only_explicit_provider_and_rejects_invalid_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OPENASI_API_KEY",
        "OPENASI_BASE_URL",
        "OPENASI_MODEL",
        "OPENASI_TIMEOUT_SECONDS",
        "OPENASI_STRUCTURED_OUTPUTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    with pytest.raises(ModelError, match="model_key_not_configured"):
        OpenASISettings.from_env()
    monkeypatch.setenv("OPENASI_API_KEY", FAKE_KEY)
    assert OpenASISettings.from_env().model == MODEL
    for name, value in [
        ("OPENASI_TIMEOUT_SECONDS", "nan"),
        ("OPENASI_STRUCTURED_OUTPUTS", "yes"),
        ("OPENASI_MODEL", "another-model"),
    ]:
        with monkeypatch.context() as context:
            context.setenv(name, value)
            with pytest.raises(ModelError, match="invalid_model_configuration"):
                OpenASISettings.from_env()


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "model_authentication_failed"),
        (403, "model_authentication_failed"),
        (429, "model_rate_limited"),
        (503, "model_unavailable"),
        (400, "model_request_rejected"),
        (302, "model_request_rejected"),
    ],
)
def test_http_failures_are_sanitized_and_not_retried(status: int, code: str) -> None:
    count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(
            status, text="PRIVATE-RESPONSE-" + FAKE_KEY, headers={"Location": "https://evil.test"}
        )

    async def scenario() -> None:
        client = OpenASIClient(OpenASISettings(FAKE_KEY), transport=httpx.MockTransport(handle))
        with pytest.raises(ModelError) as caught:
            await client.generate(REQUEST)
        assert str(caught.value) == code
        assert caught.value.__cause__ is None

    asyncio.run(scenario())
    assert count == 1


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
        parse_response(body)


def test_unknown_usage_is_none_not_zero() -> None:
    result = parse_response(response_body(usage={"input_tokens": True, "output_tokens": -1}))
    assert result.usage.input_tokens is None and result.usage.output_tokens is None


@pytest.mark.parametrize(
    "body,code",
    [
        (b"{", "invalid_model_response"),
        (b"x" * (MAX_RESPONSE_BYTES + 1), "model_response_too_large"),
    ],
    ids=["invalid-json", "oversized-body"],
)
def test_bad_or_oversized_body(body: bytes, code: str) -> None:
    async def scenario() -> None:
        client = OpenASIClient(
            OpenASISettings(FAKE_KEY),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
        )
        with pytest.raises(ModelError, match=code):
            await client.generate(REQUEST)

    asyncio.run(scenario())


def test_schema_is_explicit_opt_in_no_compatibility_fallback() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["text"]["format"]["type"] == "json_schema"
        return httpx.Response(400)

    async def scenario() -> None:
        client = OpenASIClient(
            OpenASISettings(FAKE_KEY, structured_outputs=True),
            transport=httpx.MockTransport(handle),
        )
        with pytest.raises(ModelError, match="model_request_rejected"):
            await client.generate(replace(REQUEST, response_schema={"type": "object"}))

    asyncio.run(scenario())


def test_timeout_error_has_no_raw_details() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("PRIVATE-" + FAKE_KEY, request=request)

    async def scenario() -> None:
        client = OpenASIClient(OpenASISettings(FAKE_KEY), transport=httpx.MockTransport(handle))
        with pytest.raises(ModelError, match="^model_timeout$"):
            await client.generate(REQUEST)

    asyncio.run(scenario())


def test_cancellation_propagates_without_next_request() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        calls = 0

        async def handle(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("不能继续请求")

        client = OpenASIClient(OpenASISettings(FAKE_KEY), transport=httpx.MockTransport(handle))
        task = asyncio.create_task(client.generate(REQUEST))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("tokens", [True, 0, 4097])
def test_invalid_request_has_no_http_call(tokens: int) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("非法请求不发送")

    async def scenario() -> None:
        client = OpenASIClient(OpenASISettings(FAKE_KEY), transport=httpx.MockTransport(handle))
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
        client = OpenASIClient(OpenASISettings(FAKE_KEY), transport=httpx.MockTransport(handle))
        with pytest.raises(ModelError, match="invalid_model_request"):
            await client.generate(replace(REQUEST, **changes))

    asyncio.run(scenario())


def test_total_timeout_and_client_cleanup() -> None:
    class WaitingTransport(httpx.AsyncBaseTransport):
        closed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.Event().wait()
            raise AssertionError("总超时后不可继续")

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        transport = WaitingTransport()
        client = OpenASIClient(OpenASISettings(FAKE_KEY, timeout_seconds=1), transport=transport)
        with pytest.raises(ModelError, match="model_timeout"):
            await client.generate(REQUEST)
        assert transport.closed

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
        lambda: OpenASIClient(OpenASISettings(FAKE_KEY), transport=httpx.MockTransport(handle)),
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
    def unavailable() -> OpenASIClient:
        raise ModelError("model_key_not_configured")

    monkeypatch.setattr(probe_module, "configured_client", unavailable)
    assert probe_module.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "model_key_not_configured",
    }
