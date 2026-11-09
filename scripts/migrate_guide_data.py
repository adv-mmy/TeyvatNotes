"""一次性离线转换：旧库只读，默认仅预检。"""

import argparse
import asyncio
import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Table, and_, insert, or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from teyvatnotes.application.conversations import ConversationError, GuideConversation
from teyvatnotes.database import Base, create_database, normalize_database_url
from teyvatnotes.repositories.conversation_codec import conversation_from_data
from teyvatnotes.repositories.conversation_sqlite import SqliteConversationRepository

_TABLES = (
    ("guide_sources", ("platform", "native_id", "part_id")),
    ("guide_documents", ("source_id", "content_hash", "processing_version")),
    ("guide_chunks", ("document_id", "ordinal")),
    ("guide_ingestion_steps", ()),
)
_MAX_CONVERSATIONS = 2000


class MigrationError(Exception):
    """不输出旧数据库、历史或凭据。"""


class _Conflicts(Exception):
    def __init__(self, report: dict[str, dict[str, int]]) -> None:
        self.report = report


def _url(name: str) -> str:
    value = normalize_database_url(os.environ.get(name, ""))
    parsed = make_url(value)
    if parsed.drivername != "postgresql+asyncpg" or not parsed.database:
        raise MigrationError("invalid_database_configuration")
    return value


def _target_path() -> Path:
    override = os.environ.get("TEYVAT_NOTES_DATA_DIR")
    if override:
        root = Path(override)
    else:
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        root /= "TeyvatNotes"
    return root / "client" / "conversations.sqlite3"


async def _copy_table(
    source: AsyncConnection,
    target: AsyncConnection,
    table: Table,
    natural_key: tuple[str, ...],
    *,
    apply: bool,
) -> dict[str, int]:
    counts = {"inserted": 0, "unchanged": 0, "conflicts": 0}
    primary = tuple(table.primary_key.columns)
    async with source.stream(select(table).order_by(*primary)) as rows:
        async for row in rows.mappings():
            identities = [and_(*(column == row[column.name] for column in primary))]
            if natural_key:
                identities.append(and_(*(table.c[key] == row[key] for key in natural_key)))
            actual = (
                (await target.execute(select(table).where(or_(*identities)).limit(2)))
                .mappings()
                .all()
            )
            values = dict(row)
            if not actual:
                if apply:
                    await target.execute(insert(table).values(values))
                counts["inserted"] += 1
            elif len(actual) == 1 and dict(actual[0]) == values:
                counts["unchanged"] += 1
            else:
                counts["conflicts"] += 1
    return counts


async def migrate_guides(
    source_url: str,
    target_url: str,
    *,
    apply: bool = False,
) -> dict[str, dict[str, int]]:
    """只复制产品所需四表；保留 ID 和版本，不重新解析或改写来源。"""
    source = create_database(source_url)
    try:
        target = create_database(target_url)
        try:
            report: dict[str, dict[str, int]] = {}
            async with source.engine.begin() as before, target.engine.begin() as after:
                await before.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
                if not apply:
                    await after.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    )
                # 即使经不同代理/地址连到同一实例，也拒绝同名库；不靠 URL 字符串判断。
                identity = text("SELECT current_database()")
                if await before.scalar(identity) == await after.scalar(identity):
                    raise MigrationError("same_source_and_target")
                schema = await after.scalar(text("SELECT version_num FROM alembic_version"))
                if schema != "20261109_guide_0001":
                    raise MigrationError("target_not_product_schema")
                if apply:
                    await after.execute(text("SELECT pg_advisory_xact_lock(3185430820)"))
                for table_name, natural_key in _TABLES:
                    counts = await _copy_table(
                        before, after, Base.metadata.tables[table_name], natural_key, apply=apply
                    )
                    report[table_name] = counts
                    if apply and counts["conflicts"]:
                        raise _Conflicts(report)
            return report
        finally:
            await target.dispose()
    finally:
        await source.dispose()


def _decode_rows(rows: Iterable[Mapping[str, Any]], owner: UUID) -> tuple[GuideConversation, ...]:
    result: list[GuideConversation] = []
    for row in rows:
        raw = row["payload"]
        value = conversation_from_data(json.loads(raw) if isinstance(raw, str) else raw)
        if (
            str(value.id) != str(row["id"])
            or value.owner_id != owner
            or str(value.owner_id) != str(row["owner_id"])
            or value.revision != row["revision"]
        ):
            raise MigrationError("invalid_legacy_conversation")
        if len(result) >= _MAX_CONVERSATIONS:
            raise MigrationError("conversation_import_limit")
        if (turn := value.active_turn) is not None:
            finished = max(value.updated_at, turn.started_at)
            interrupted = replace(
                turn,
                status="interrupted",
                finished_at=finished,
                reply={"status": "error", "reason": "conversation_interrupted"},
            )
            value = replace(
                value,
                turns=(*value.turns[:-1], interrupted),
                revision=value.revision + 1,
                updated_at=finished,
            )
        result.append(value)
    return tuple(result)


def read_sqlite(path: Path) -> tuple[GuideConversation, ...]:
    """只接受旧本地会话格式，不打开新版目标作为源。"""
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise MigrationError("unsupported_legacy_sqlite")
        row = db.execute("SELECT owner_id FROM local_profile WHERE slot = 1").fetchone()
        if row is None:
            raise MigrationError("missing_legacy_profile")
        owner = UUID(row[0])
        rows = db.execute(
            "SELECT id, owner_id, revision, payload FROM guide_conversations "
            "WHERE owner_id = ? ORDER BY id LIMIT ?",
            (str(owner), _MAX_CONVERSATIONS + 1),
        )
        return _decode_rows((dict(item) for item in rows), owner)


async def read_postgres(source_url: str, owner: UUID) -> tuple[GuideConversation, ...]:
    database = create_database(source_url)
    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            rows = await connection.execute(
                text(
                    "SELECT id, owner_id, revision, payload FROM guide_conversations "
                    "WHERE owner_id = :owner ORDER BY id LIMIT :cap"
                ),
                {"owner": owner, "cap": _MAX_CONVERSATIONS + 1},
            )
            return _decode_rows((dict(row) for row in rows.mappings()), owner)
    finally:
        await database.dispose()


async def _run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    mode = "apply" if args.apply else "dry_run"
    if args.kind == "guides":
        try:
            report = await migrate_guides(
                _url("GUIDE_LEGACY_DATABASE_URL"), _url("GUIDE_DATABASE_URL"), apply=args.apply
            )
        except _Conflicts as error:
            rolled_back = {table: {**row, "inserted": 0} for table, row in error.report.items()}
            return {"mode": mode, "status": "conflict", "tables": rolled_back}, 1
        failed = any(row["conflicts"] for row in report.values())
        return {"mode": mode, "status": "conflict" if failed else "ok", "tables": report}, int(
            failed
        )
    target = args.target
    if args.sqlite is not None:
        if args.sqlite.resolve() == target.resolve():
            raise MigrationError("same_source_and_target")
        values = await asyncio.to_thread(read_sqlite, args.sqlite)
    else:
        values = await read_postgres(_url("GUIDE_LEGACY_DATABASE_URL"), args.owner)
    result = await SqliteConversationRepository(target).import_conversations(
        values, dry_run=not args.apply
    )
    return {"mode": mode, **asdict(result)}, int(result.conflicts != 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读检查或一次性迁移旧攻略与私有会话")
    modes = parser.add_subparsers(dest="kind", required=True)
    guides = modes.add_parser("guides")
    guides.add_argument("--apply", action="store_true")
    conversations = modes.add_parser("conversations")
    source = conversations.add_mutually_exclusive_group(required=True)
    source.add_argument("--sqlite", type=Path)
    source.add_argument("--postgres", action="store_true")
    conversations.add_argument("--owner", type=UUID)
    conversations.add_argument("--target", type=Path, default=_target_path())
    conversations.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.kind == "conversations" and (args.postgres != (args.owner is not None)):
        parser.error("只有 PostgreSQL 源需要 --owner UUID")
    try:
        result, status = asyncio.run(_run(args))
    except MigrationError as error:
        result, status = {"status": "error", "code": str(error)}, 1
    except (SQLAlchemyError, OSError, sqlite3.Error, ValueError, ConversationError):
        result, status = {"status": "error", "code": "migration_unavailable"}, 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
