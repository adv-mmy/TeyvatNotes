"""构建和读取随桌面应用交付的小型攻略库。"""

import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.desktop.config import DesktopError
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository

DATA = Path(__file__).resolve().parents[1] / "knowledge" / "data"
LIBRARY_NAME = "desktop_guides.sqlite3"
STARTER_QUESTION = "薇斯纳怎么配队？"
STARTER_VERSION = "7.1"
MAX_GUIDES = 100


def _source_records() -> list[str]:
    # 开发运行与构建共用一份明确选入的资料，不扫描项目或读取业务库。
    value = json.loads((DATA / "desktop_guides.json").read_text(encoding="utf-8"))
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_GUIDES:
        raise DesktopError("local_library_unavailable")
    return [json.dumps(item, ensure_ascii=False, allow_nan=False) for item in value]


def _bundled_records(path: Path) -> list[str]:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise DesktopError("local_library_unavailable")
        rows = db.execute(
            "SELECT snapshot FROM guide_snapshots ORDER BY id LIMIT ?", (MAX_GUIDES + 1,)
        ).fetchall()
        if not 1 <= len(rows) <= MAX_GUIDES or any(not isinstance(row[0], str) for row in rows):
            raise DesktopError("local_library_unavailable")
        return [str(row[0]) for row in rows]


async def _reader(records: list[str]) -> KnowledgeReader:
    # 小型资料包重用现有入库校验、分块与检索规则，不复制一套问答逻辑。
    repository = InMemoryKnowledgeRepository()
    importer = GuideIngestionService(repository)
    for raw in records:
        await importer.import_snapshot(parse_snapshot(json.loads(raw)), publish=True)
    return repository


async def load_library() -> KnowledgeReader:
    """安装版必须读只读包；源码运行使用同一批受控输入。"""
    path = DATA / LIBRARY_NAME
    try:
        if getattr(sys, "frozen", False) or path.exists():
            records = await asyncio.to_thread(_bundled_records, path)
        else:
            records = await asyncio.to_thread(_source_records)
        return await _reader(records)
    except (OSError, sqlite3.Error, ValueError, GuideImportError):
        raise DesktopError("local_library_unavailable") from None


async def build_library(path: Path) -> None:
    """从资料清单生成安装包数据库；先经过正式发布校验，不导入本机数据。"""
    records = _source_records()
    await _reader(records)
    if path.exists():
        raise DesktopError("local_library_already_exists")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN")
        db.execute("CREATE TABLE guide_snapshots (id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL)")
        db.executemany("INSERT INTO guide_snapshots (snapshot) VALUES (?)", [(x,) for x in records])
        db.execute("PRAGMA user_version = 1")


if __name__ == "__main__":
    asyncio.run(build_library(Path(sys.argv[1])))
