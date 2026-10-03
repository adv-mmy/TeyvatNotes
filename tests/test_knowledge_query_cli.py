"""只读 CLI 输入预算、演示边界及稳定错误，不输出凭据或玩家原文。"""

import json
import sys
from io import BytesIO, TextIOWrapper
from pathlib import Path

import pytest

from agent_runtime_foundation.domain.guides import GuideImportError
from agent_runtime_foundation.knowledge.output import emit_json
from agent_runtime_foundation.knowledge.query_cli import main, parse_conditions
from agent_runtime_foundation.knowledge.questions import MAX_QUESTION_BYTES, load_question

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEARCH = [
    "search",
    "--version",
    "DEMO-ONLY",
    "--kind",
    "character",
    "--entity",
    "示例角色甲",
    "--keyword",
    "装备",
    "--condition",
    "装备=示例木剑",
]


def test_cli_demo_search_and_separate_invocation_reads_stable_evidence_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([*SEARCH, "--demo"]) == 0
    search = json.loads(capsys.readouterr().out)
    hit = search["hits"][0]
    assert search["storage"] == "memory_demo" and search["model_called"] is False
    assert hit["citation_url"].endswith("?p=2&t=10")
    assert (
        main(
            [
                "read-evidence",
                hit["evidence_id"],
                *SEARCH[1:7],
                "--condition",
                "装备=示例木剑",
                "--demo",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["hit"]["document_id"] == hit["document_id"]
    assert len(result["context"]) == 3 and not result["context_truncated"]


def test_cli_question_preparation_and_missing_database_are_explicit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["prepare-question", str(PROJECT_ROOT / "samples/questions/quest.json"), "--demo"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "evidence_ready" and "answer" not in result
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(SEARCH) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "database_not_configured"}


@pytest.mark.parametrize("values", [["x"], ["x=1", "x=2"], ["=value"], ["x="]])
def test_cli_rejects_invalid_conditions(
    values: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    command = [*SEARCH[:9]]
    for value in values:
        command.extend(["--condition", value])
    assert main([*command, "--demo"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "invalid_knowledge_query"}


@pytest.mark.parametrize(
    "content",
    [b"\xff", b"{", b" " * (MAX_QUESTION_BYTES + 1)],
    ids=["bad_utf8", "bad_json", "too_large"],
)
def test_question_loader_is_bounded(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "question.json"
    path.write_bytes(content)
    with pytest.raises(GuideImportError):
        load_question(path)


def test_invalid_question_does_not_leak_sensitive_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "private-question.json"
    path.write_text('{"question":"PRIVATE-PLAYER","api_key":"SECRET"}', encoding="utf-8")
    assert main(["prepare-question", str(path), "--demo"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "invalid_question"}


def test_conditions_preserve_equals_in_value() -> None:
    assert parse_conditions(["装备=x=y"]) == (("装备", "x=y"),)


def test_json_stdout_is_utf8_even_when_windows_pipe_defaults_to_gbk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = BytesIO()
    stream = TextIOWrapper(buffer, encoding="gbk")
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", stream)
        emit_json({"message": "中文证据"})
        stream.flush()
    assert json.loads(buffer.getvalue().decode("utf-8")) == {"message": "中文证据"}
