"""供应商无关配置与端到端 MockTransport 验证；没有真实凭据或网络。"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from teyvatnotes.application.model_ports import ModelError, ModelMessage, ModelRequest
from teyvatnotes.application.tool_model_ports import ToolDefinition, ToolTurnRequest
from teyvatnotes.models.responses import ResponsesClient, ResponsesSettings
from teyvatnotes.models.responses_tools import ResponsesToolClient

KEY = "fake-test-key"
SETTINGS = ResponsesSettings(KEY, "https://llm.example.test/v1", "example-model")


@pytest.fixture
def clean_model_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """逐测试隔离通用环境变量，不借用开发者本机配置。"""
    for name in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_STYLE",
        "LLM_TIMEOUT_SECONDS",
        "LLM_STRUCTURED_OUTPUTS",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    "base_url,endpoint",
    [
        ("https://provider-a.example/v1", "https://provider-a.example/v1/responses"),
        (
            "https://provider-b.example:8443/custom/api/",
            "https://provider-b.example:8443/custom/api/responses",
        ),
        (
            "https://provider-b.example/custom/responses/",
            "https://provider-b.example/custom/responses",
        ),
        ("https://provider-c.example", "https://provider-c.example/responses"),
    ],
)
@pytest.mark.parametrize("model", ["model-a", "vendor/model-b", "model-c-2026-10"])
def test_both_clients_use_configured_address_and_model(
    base_url: str, endpoint: str, model: str
) -> None:
    settings = ResponsesSettings(KEY, base_url, model)
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == endpoint
        assert request.headers["authorization"] == "Bearer " + KEY
        assert json.loads(request.content)["model"] == model
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "model": model,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "回答"}],
                    }
                ],
            },
        )

    async def scenario() -> None:
        transport = httpx.MockTransport(handle)
        single = await ResponsesClient(settings, transport=transport).generate(
            ModelRequest((ModelMessage("user", "你好"),), 128)
        )
        tool = await ResponsesToolClient(settings, transport=transport).generate_turn(
            ToolTurnRequest(
                (ModelMessage("user", "你好"),),
                (
                    ToolDefinition(
                        "search",
                        "搜索",
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
        assert single.model == tool.model == model
        assert single.text == tool.text == "回答"

    asyncio.run(scenario())
    assert len(calls) == 2


@pytest.mark.parametrize("name", ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"])
def test_required_configuration_has_no_provider_defaults(
    clean_model_env: pytest.MonkeyPatch,
    name: str,
) -> None:
    for key, value in {
        "LLM_API_KEY": KEY,
        "LLM_BASE_URL": SETTINGS.base_url,
        "LLM_MODEL": SETTINGS.model,
    }.items():
        clean_model_env.setenv(key, value)
    clean_model_env.delenv(name)
    code = "model_key_not_configured" if name == "LLM_API_KEY" else "invalid_model_configuration"
    with pytest.raises(ModelError, match="^" + code + "$"):
        ResponsesSettings.from_env()


def test_env_is_generic_and_does_not_read_legacy_keys(clean_model_env: pytest.MonkeyPatch) -> None:
    clean_model_env.setenv("OPENASI_API_KEY", KEY)
    clean_model_env.setenv("OPENASI_BASE_URL", SETTINGS.base_url)
    clean_model_env.setenv("OPENASI_MODEL", SETTINGS.model)
    clean_model_env.setenv("OPENAI_API_KEY", KEY)
    with pytest.raises(ModelError, match="^model_key_not_configured$"):
        ResponsesSettings.from_env()
    clean_model_env.setenv("LLM_API_KEY", KEY)
    with pytest.raises(ModelError, match="^invalid_model_configuration$"):
        ResponsesSettings.from_env()
    clean_model_env.setenv("LLM_BASE_URL", SETTINGS.base_url)
    clean_model_env.setenv("LLM_MODEL", "configured-model")
    clean_model_env.setenv("LLM_TIMEOUT_SECONDS", "15")
    clean_model_env.setenv("LLM_STRUCTURED_OUTPUTS", "true")
    result = ResponsesSettings.from_env()
    assert result.model == "configured-model" and result.timeout_seconds == 15
    assert result.structured_outputs and result.api_style == "responses"
    assert KEY not in repr(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://example.test/v1"},
        {"base_url": "https:///v1"},
        {"base_url": "https://user:secret@example.test/v1"},
        {"base_url": "https://example.test/v1?token=secret"},
        {"base_url": "https://example.test/v1#secret"},
        {"base_url": "https://example.test:99999/v1"},
        {"base_url": "https://example.test:bad/v1"},
        {"base_url": "https://example.test/\nresponses"},
        {"base_url": "https://example.test/v1/chat/completions"},
        {"model": ""},
        {"model": " model"},
        {"model": "model\n"},
        {"model": "x" * 161},
        {"api_style": "chat_completions"},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": True},
    ],
)
def test_invalid_configuration_is_sanitized(changes: dict[str, object]) -> None:
    with pytest.raises(ModelError, match="^invalid_model_configuration$"):
        replace(SETTINGS, **changes)


def test_sample_and_runtime_modules_have_no_vendor_binding() -> None:
    root = Path(__file__).resolve().parents[1]
    sample = (root / ".env.example").read_text(encoding="utf-8")
    assert "LLM_API_KEY=\n" in sample and "LLM_MODEL=\n" in sample
    assert "LLM_BASE_URL=\n" in sample and "LLM_API_STYLE=responses" in sample
    assert "OPENASI_" not in sample and "gpt-5.6-" not in sample
    for path in (root / "src/teyvatnotes").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "openasi" not in text.lower(), path
        assert "gpt-5.6-sol" not in text, path
