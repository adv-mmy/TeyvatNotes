"""私有会话CLI与有界补充输入；无真实模型请求。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from teyvatnotes.application.conversations import ConversationError
from teyvatnotes.knowledge import conversation_cli as cli

ROOT = Path(__file__).resolve().parents[1]


def test_demo_clarifies_then_replaces_inventory_and_evidence_with_zero_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", "PRIVATE")
    monkeypatch.setenv("GUIDE_SESSION_OWNER_ID", "PRIVATE")
    monkeypatch.setenv("LLM_API_KEY", "PRIVATE")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("不能读数据库或模型")

    monkeypatch.setattr(cli, "create_database", forbidden)
    monkeypatch.setattr(cli, "configured_tool_client", forbidden)
    assert cli.main(["demo"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["model_called"] is False and result["demo_only"] is True
    steps = result["steps"]
    assert [step["revision"] for step in steps] == [2, 4, 6]
    assert [step["latest_reply"]["status"] for step in steps] == [
        "needs_clarification",
        "answer",
        "answer",
    ]
    assert steps[1]["context"]["owned_equipment"] == ["示例木剑"]
    assert steps[2]["context"]["owned_equipment"] == ["示例木弓"]
    before = {
        identifier
        for claim in steps[1]["latest_reply"]["claims"]
        for identifier in claim["evidence_ids"]
    }
    after = {
        identifier
        for claim in steps[2]["latest_reply"]["claims"]
        for identifier in claim["evidence_ids"]
    }
    assert before and after and before.isdisjoint(after)
    assert "PRIVATE" not in output and "reasoning" not in output
    assert "tool_calls" not in output
    assert len(result["conversation"]["history"]) == 3


@pytest.mark.parametrize(
    "text,code",
    [
        ('{"question":"一","question":"二"}', "conversation_input_unreadable"),
        ('{"question":"问","conditions":{"x":"一","x":"二"}}', "conversation_input_unreadable"),
        ('{"question":"问","goal":NaN}', "conversation_input_unreadable"),
        ("{", "conversation_input_unreadable"),
        ("[]", "invalid_conversation_input"),
        ('{"question":"问","guide_kind":"other"}', "invalid_conversation_input"),
        ('{"question":"问","api_key":"PRIVATE"}', "invalid_conversation_input"),
        (" " * 16_385, "conversation_input_too_large"),
    ],
)
def test_patch_file_rejects_ambiguous_or_oversized_json(
    tmp_path: Path, text: str, code: str
) -> None:
    file = tmp_path / "patch.json"
    file.write_text(text, encoding="utf-8")
    with pytest.raises(ConversationError, match="^" + code + "$"):
        cli.load_patch(file)


def test_bom_supported_missing_file_error_is_sanitized(tmp_path: Path) -> None:
    file = tmp_path / "patch.json"
    file.write_bytes(b'\xef\xbb\xbf{"question":"ask","owned_equipment":[]}')
    assert cli.load_patch(file).values["owned_equipment"] == []
    with pytest.raises(ConversationError, match="conversation_input_unreadable"):
        cli.load_patch(tmp_path / "PRIVATE")


def test_owner_and_business_database_are_explicit_never_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GUIDE_SESSION_OWNER_ID", raising=False)
    monkeypatch.setenv("TEST_DATABASE_URL", "PRIVATE")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["show", str(uuid4())]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "conversation_owner_not_configured"
    monkeypatch.setenv("GUIDE_SESSION_OWNER_ID", str(uuid4()))
    assert cli.main(["show", str(uuid4())]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "database_not_configured"
    assert cli.main(["show", "PRIVATE"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_conversation_id"


def test_demo_context_and_tools_are_new_on_each_round() -> None:
    result = asyncio.run(cli.run_demo())
    steps = result["steps"]
    assert isinstance(steps, list) and len(steps) == 3
    for step in steps:
        assert isinstance(step, dict)
        assert step["demo_only"] is True
