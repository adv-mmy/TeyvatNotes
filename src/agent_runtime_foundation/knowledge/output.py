"""知识 CLI 共用的 UTF-8 JSON 输出，兼容 Windows 管道与测试捕获。"""

import json
import sys
from io import TextIOWrapper

from agent_runtime_foundation.domain.task import JsonValue


def emit_json(value: dict[str, JsonValue]) -> None:
    """仅重配置实际标准文本流；StringIO/capsys 保留 Unicode 捕获行为。"""
    stream = sys.stdout
    if isinstance(stream, TextIOWrapper):
        stream.reconfigure(encoding="utf-8")
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), file=stream)
