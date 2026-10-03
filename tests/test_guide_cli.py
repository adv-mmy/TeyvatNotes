"""离线 CLI 输出、文件输入边界与凭据不泄漏测试。"""

import json
from pathlib import Path

import pytest

from agent_runtime_foundation.domain.guides import GuideImportError
from agent_runtime_foundation.knowledge.cli import main
from agent_runtime_foundation.knowledge.snapshots import MAX_SNAPSHOT_BYTES, load_snapshot

SAMPLE = Path(__file__).resolve().parents[1] / "samples/guides/bilibili_character.json"


def test_cli_memory_import_has_explicit_storage_and_gaps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["import-snapshot", str(SAMPLE), "--memory"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["storage"] == "memory"
    assert output["status"] == "draft"
    assert output["chunk_count"] == 3
    assert output["blocking_issues"] == ["version_unknown", "fixture_source"]


def test_cli_blocks_publication_and_prints_only_stable_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["import-snapshot", str(SAMPLE), "--memory", "--publish"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "publication_blocked"}


def test_cli_does_not_silently_replace_missing_database_with_memory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["import-snapshot", str(SAMPLE)]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "database_not_configured"}


@pytest.mark.parametrize(
    "content",
    [b"\xff", b"{invalid-json", b" " * (MAX_SNAPSHOT_BYTES + 1)],
    ids=["bad_utf8", "bad_json", "too_large"],
)
def test_file_loader_is_bounded_and_rejects_bad_utf8(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "snapshot.json"
    path.write_bytes(content)
    with pytest.raises(GuideImportError):
        load_snapshot(path)


def test_cli_invalid_snapshot_does_not_expose_secret_or_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "private-snapshot.json"
    path.write_text('{"api_key":"SECRET-DO-NOT-EXPOSE"}', encoding="utf-8")
    assert main(["import-snapshot", str(path), "--memory"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "invalid_snapshot"}
