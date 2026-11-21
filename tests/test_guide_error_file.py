"""私有 JSONL 的重启追加、进程内读取、有限轮转与写入失败隔离。"""

import json
from pathlib import Path

import pytest

from teyvatnotes.api.error_log import GuideErrorFile


def record(file: GuideErrorFile, index: int) -> None:
    file.write(
        time="2026-11-21T14:05:06+00:00",
        request_id=f"id-{index}",
        path="local",
        status=502,
        error_code="invalid_answer_structure",
        elapsed_ms=5,
        validation_detail="json_invalid",
        model_output=f"第{index}条\r\n非 JSON 的隐私正文",
    )


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_file_appends_across_restart_and_rotates(tmp_path: Path) -> None:
    folder = tmp_path / "server" / "logs"
    file = GuideErrorFile(folder)
    try:
        record(file, 0)
        assert records(file.path)[0]["model_output"] == "第0条\r\n非 JSON 的隐私正文"
    finally:
        file.close()
    reopened = GuideErrorFile(folder)
    try:
        record(reopened, 1)
        assert [row["request_id"] for row in records(reopened.path)] == ["id-0", "id-1"]
    finally:
        reopened.close()
    rotating = GuideErrorFile(folder, max_bytes=150, backups=2)
    try:
        for index in range(2, 6):
            record(rotating, index)
        assert [p.name for p in sorted(folder.iterdir())] == [
            "guide-errors.jsonl",
            "guide-errors.jsonl.1",
            "guide-errors.jsonl.2",
        ]
        for suffix, index in [("", 5), (".1", 4), (".2", 3)]:
            [event] = records(folder / ("guide-errors.jsonl" + suffix))
            assert event["model_output"] == f"第{index}条\r\n非 JSON 的隐私正文"
    finally:
        rotating.close()


def test_write_failure_never_prints_record(tmp_path, monkeypatch, capsys) -> None:
    from logging.handlers import RotatingFileHandler

    def unavailable(self, record):
        raise OSError("模拟写失败")

    file = GuideErrorFile(tmp_path)
    try:
        monkeypatch.setattr(RotatingFileHandler, "shouldRollover", unavailable)
        with pytest.raises(OSError, match="^guide_error_log_unavailable$"):
            record(file, 1)
        captured = capsys.readouterr()
        assert not captured.err and not captured.out
    finally:
        file.close()
