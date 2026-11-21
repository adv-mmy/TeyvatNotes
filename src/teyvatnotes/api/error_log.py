"""单实例服务的本机诊断文件，不向普通 logger 分发原始回答。"""

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def error_log_directory() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "TeyvatNotes" / "server" / "logs"


class _PrivateHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # 标准 logging 的默认错误处理可能向 stderr 打印 record.msg（包含整份正文）。
        raise OSError("guide_error_log_unavailable") from None


class GuideErrorFile:
    def __init__(
        self, directory: Path, *, max_bytes: int = 5 * 1024 * 1024, backups: int = 3
    ) -> None:
        directory = directory.resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "guide-errors.jsonl"
        self._handler = _PrivateHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
            errors="backslashreplace",
        )

    def write(
        self,
        *,
        time: str,
        request_id: str,
        path: str | None,
        status: int,
        error_code: str,
        elapsed_ms: int,
        validation_detail: str | None,
        model_output: str | None,
    ) -> None:
        event = {
            "time": time,
            "request_id": request_id,
            "path": path,
            "status": status,
            "error_code": error_code,
            "elapsed_ms": elapsed_ms,
            "validation_detail": validation_detail,
            "model_output": model_output if model_output is not None else "未收到模型输出",
        }
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        # handle() 串行保护轮转和写入，emit() 每条刷新；不注册到根日志。
        self._handler.handle(
            logging.LogRecord("guide_errors", logging.ERROR, "", 0, line, (), None)
        )

    def close(self) -> None:
        self._handler.close()
