"""开发者离线名称检查入口；仅检查/显示，不覆写快照、不连接数据库。"""

import argparse
from pathlib import Path

from teyvatnotes.application.terminology import (
    correct_blocks,
    prepare_terminology,
    resolution_to_json,
)
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.task import JsonValue
from teyvatnotes.domain.terminology import TermCategory
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.knowledge.parsing import OfflineSnapshotParser, blocks_to_json
from teyvatnotes.knowledge.snapshots import load_snapshot
from teyvatnotes.knowledge.terminology import load_catalog


def run_command(arguments: argparse.Namespace) -> dict[str, JsonValue]:
    """未知/近似词原样显示候选，不将相似度当作已经确认的纠错。"""
    catalog = load_catalog(Path(arguments.catalog) if arguments.catalog else None)
    metadata: dict[str, JsonValue] = {
        "catalog_revision": catalog.revision,
        "catalog_hash": catalog.content_hash,
        "model_called": False,
    }
    if arguments.command == "check-name":
        return {
            **metadata,
            **resolution_to_json(catalog.resolve(arguments.name, TermCategory(arguments.category))),
        }
    snapshot = prepare_terminology(load_snapshot(Path(arguments.path)), catalog)
    blocks, ambiguous = correct_blocks(OfflineSnapshotParser().parse(snapshot), catalog)
    return {
        **metadata,
        "terminology": snapshot.data["_terminology"],
        "ambiguous_names": list(ambiguous),
        "blocks": blocks_to_json(blocks),
        "note": "未写文件或入库；正文未知名称须先由人工/识别器显式标注recognized_terms。",
    }


def main(argv: list[str] | None = None) -> int:
    """不输出内部异常/文件路径；名词正文只用于本机开发者检查，不接公开API。"""
    parser = argparse.ArgumentParser(description="人工维护专名校对：无模型/网络/数据库")
    commands = parser.add_subparsers(dest="command", required=True)
    name = commands.add_parser("check-name")
    name.add_argument("name")
    name.add_argument(
        "--category", choices=[category.value for category in TermCategory], required=True
    )
    snapshot = commands.add_parser("check-snapshot")
    snapshot.add_argument("path")
    for command in (name, snapshot):
        command.add_argument("--catalog", help="开发者UTF-8词表路径；默认使用包内词表")
    arguments = parser.parse_args(argv)
    try:
        result = run_command(arguments)
    except GuideImportError as exc:
        emit_json({"status": "error", "error": exc.code})
        return 1
    except Exception:
        emit_json({"status": "error", "error": "terminology_check_failed"})
        return 1
    emit_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
