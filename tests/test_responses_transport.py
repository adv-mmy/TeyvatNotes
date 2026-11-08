"""两个现有生成入口共用的 HTTP 合同；只使用本地 transport 替身。"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest

from teyvatnotes.application.model_ports import (
    ModelError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)
from teyvatnotes.application.tool_model_ports import (
    ToolDefinition,
    ToolTurnRequest,
    ToolTurnResponse,
)
from teyvatnotes.models.responses import (
    MAX_RESPONSE_BYTES,
    ResponsesClient,
    ResponsesSettings,
)
from teyvatnotes.models.responses_tools import ResponsesToolClient

type Generate = Callable[
    [httpx.AsyncBaseTransport, float], Awaitable[ModelResponse | ToolTurnResponse]
]
RESPONSE = json.dumps(
    {
        "status": "completed",
        "model": "example-model-v1",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
    }
).encode()


@pytest.fixture(params=["text", "tool"])
def generate(request: pytest.FixtureRequest) -> Generate:
    """合同通过实际生成 interface 运行，不直接调用共享传输实现。"""

    async def call(
        transport: httpx.AsyncBaseTransport, timeout: float
    ) -> ModelResponse | ToolTurnResponse:
        settings = ResponsesSettings(
            "test-only-not-a-real-api-key",
            "https://llm.example.test/v1",
            "example-model-v1",
            timeout_seconds=timeout,
        )
        messages = (ModelMessage("user", "你好"),)
        if request.param == "text":
            return await ResponsesClient(settings, transport=transport).generate(
                ModelRequest(messages, 100)
            )
        return await ResponsesToolClient(settings, transport=transport).generate_turn(
            ToolTurnRequest(
                messages,
                (
                    ToolDefinition(
                        "search_knowledge",
                        "只读查询。",
                        {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                    ),
                ),
            )
        )

    return call


class ClosingTransport(httpx.MockTransport):
    closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


class ChunkedStream(httpx.AsyncByteStream):
    """保留逐块读取与关闭行为，避免预加载正文掩盖读流问题。"""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.read = False
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


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
def test_http_errors_skip_body_and_close_without_retry(
    generate: Generate, status: int, code: str
) -> None:
    async def scenario() -> None:
        stream = ChunkedStream(b"PRIVATE-RESPONSE-test-only-not-a-real-api-key")
        calls = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(status, stream=stream, headers={"Location": "https://evil.test"})

        transport = ClosingTransport(handle)
        with pytest.raises(ModelError) as caught:
            await generate(transport, 60)
        assert str(caught.value) == code and caught.value.__cause__ is None
        assert calls == 1 and not stream.read
        assert stream.closed and transport.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [b"{", b"\xff", b'{"status":"failed"}'])
def test_invalid_json_or_protocol_still_closes_resources(generate: Generate, raw: bytes) -> None:
    async def scenario() -> None:
        stream = ChunkedStream(raw)
        transport = ClosingTransport(lambda request: httpx.Response(200, stream=stream))
        with pytest.raises(ModelError, match="^invalid_model_response$"):
            await generate(transport, 60)
        assert stream.closed and transport.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("excess", [0, 1])
def test_response_limit_counts_all_chunks_and_accepts_exact_limit(
    generate: Generate, excess: int
) -> None:
    async def scenario() -> None:
        raw = RESPONSE + b" " * (MAX_RESPONSE_BYTES - len(RESPONSE) + excess)
        stream = ChunkedStream(raw[: MAX_RESPONSE_BYTES // 2], raw[MAX_RESPONSE_BYTES // 2 :])
        transport = ClosingTransport(lambda request: httpx.Response(200, stream=stream))
        if excess:
            with pytest.raises(ModelError, match="^model_response_too_large$"):
                await generate(transport, 60)
        else:
            assert (await generate(transport, 60)).text == "ok"
        assert stream.closed and transport.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["request", "read"])
@pytest.mark.parametrize(
    "error_type,code",
    [(httpx.ReadTimeout, "model_timeout"), (httpx.ConnectError, "model_unavailable")],
)
def test_network_errors_are_sanitized_and_close_resources(
    generate: Generate, phase: str, error_type: type[httpx.HTTPError], code: str
) -> None:
    async def scenario() -> None:
        class BrokenStream(ChunkedStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b"{"
                raise error_type("PRIVATE-RESPONSE")

        stream = BrokenStream()
        calls = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if phase == "request":
                raise error_type("PRIVATE-REQUEST", request=request)
            return httpx.Response(200, stream=stream)

        transport = ClosingTransport(handle)
        with pytest.raises(ModelError) as caught:
            await generate(transport, 60)
        assert str(caught.value) == code and caught.value.__cause__ is None
        assert calls == 1 and transport.closed
        assert stream.closed is (phase == "read")

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["request", "read"])
@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
def test_timeout_and_cancellation_close_resources(
    generate: Generate, phase: str, cancel: bool
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        class WaitingStream(ChunkedStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b"{"
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError("停止后不能继续读流")

        stream = WaitingStream()
        calls = 0

        async def handle(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if phase == "request":
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError("停止后不能继续请求")
            return httpx.Response(200, stream=stream)

        transport = ClosingTransport(handle)
        if cancel:
            task = asyncio.create_task(generate(transport, 60))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ModelError, match="^model_timeout$"):
                await generate(transport, 1)
        assert entered.is_set() and calls == 1 and transport.closed
        assert stream.closed is (phase == "read")

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["response", "transport"])
@pytest.mark.parametrize(
    "error_type,code",
    [(TimeoutError, "model_timeout"), (httpx.ReadError, "model_unavailable")],
)
def test_cleanup_errors_remain_inside_error_mapping(
    generate: Generate, phase: str, error_type: type[Exception], code: str
) -> None:
    async def scenario() -> None:
        class BrokenStream(ChunkedStream):
            async def aclose(self) -> None:
                await super().aclose()
                if phase == "response":
                    raise error_type("PRIVATE-CLEANUP")

        class BrokenTransport(ClosingTransport):
            async def aclose(self) -> None:
                await super().aclose()
                if phase == "transport":
                    raise error_type("PRIVATE-CLEANUP")

        stream = BrokenStream(RESPONSE)
        transport = BrokenTransport(lambda request: httpx.Response(200, stream=stream))
        with pytest.raises(ModelError) as caught:
            await generate(transport, 60)
        assert str(caught.value) == code and caught.value.__cause__ is None
        assert stream.closed and transport.closed

    asyncio.run(scenario())
