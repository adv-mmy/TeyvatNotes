"""离线会话转换与独立 PostgreSQL 攻略复制。"""

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from test_guide_ingestion import article_snapshot
from test_guide_postgres import migrate

from scripts.migrate_guide_data import MigrationError, migrate_guides, read_postgres, read_sqlite
from teyvatnotes.application.conversations import ConversationTurn, GuideConversation
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.database import GuideDocumentRecord, create_database, normalize_database_url
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.conversation_codec import conversation_to_data
from teyvatnotes.repositories.conversation_sqlite import SqliteConversationRepository
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository


def _pending() -> GuideConversation:
    now = datetime.now(UTC)
    request = QuestionRequest("钟离怎么配队？")
    return GuideConversation(
        id=uuid4(),
        owner_id=uuid4(),
        context=request,
        revision=1,
        turns=(ConversationTurn(uuid4(), 1, request, now),),
        created_at=now,
        updated_at=now,
    )


def test_offline_sqlite_history_is_read_only_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    source, target = tmp_path / "old.sqlite3", tmp_path / "client" / "conversations.sqlite3"
    value = _pending()
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("PRAGMA user_version = 1")
        db.execute("CREATE TABLE local_profile (slot INTEGER PRIMARY KEY, owner_id TEXT)")
        db.execute("INSERT INTO local_profile VALUES (1, ?)", (str(value.owner_id),))
        db.execute(
            "CREATE TABLE guide_conversations (id TEXT, owner_id TEXT, revision INT, payload TEXT)"
        )
        db.execute(
            "INSERT INTO guide_conversations VALUES (?, ?, ?, ?)",
            (
                str(value.id),
                str(value.owner_id),
                value.revision,
                json.dumps(conversation_to_data(value)),
            ),
        )
    original = source.read_bytes()

    async def run() -> None:
        values = read_sqlite(source)
        assert values[0].turns[0].status == "interrupted"
        repo = SqliteConversationRepository(target)
        assert (await repo.import_conversations(values)).inserted == 1
        assert not target.exists()
        assert (await repo.import_conversations(values, dry_run=False)).inserted == 1
        assert (await repo.import_conversations(values, dry_run=False)).unchanged == 1
        owner = await repo.initialize()
        actual = await repo.get(owner, value.id)
        assert actual is not None and actual.turns == values[0].turns
        collision = replace(values[0], revision=values[0].revision + 1)
        assert (await repo.import_conversations((collision,), dry_run=False)).conflicts == 1
        assert await repo.get(owner, value.id) == actual

    asyncio.run(run())
    assert source.read_bytes() == original


@pytest.mark.postgres
def test_copy_guides_and_owner_from_independent_databases() -> None:
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("需要专用 TEST_DATABASE_URL")
    url = make_url(normalize_database_url(raw))
    names = ("guides_src_" + uuid4().hex, "guides_dst_" + uuid4().hex)

    async def run() -> None:
        admin = create_database(raw)
        try:
            async with admin.engine.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                for name in names:
                    await connection.execute(text(f'CREATE DATABASE "{name}"'))
        finally:
            await admin.dispose()
        source_url, target_url = (
            url.set(database=name).render_as_string(hide_password=False) for name in names
        )
        source, target = create_database(source_url), create_database(target_url)
        try:
            for db in (source, target):
                async with db.engine.begin() as connection:
                    await connection.run_sync(migrate)
            repo = PostgresKnowledgeRepository(source.session_factory)
            snapshot = parse_snapshot(article_snapshot())
            report = await GuideIngestionService(repo).import_snapshot(snapshot)
            preview = await migrate_guides(source_url, target_url)
            assert preview["guide_documents"]["inserted"] == 1
            async with target.session_factory() as session:
                assert await session.get(GuideDocumentRecord, report.document_id) is None
            assert (await migrate_guides(source_url, target_url, apply=True))["guide_documents"][
                "inserted"
            ] == 1
            assert (await migrate_guides(source_url, target_url, apply=True))["guide_documents"][
                "unchanged"
            ] == 1
            assert await PostgresKnowledgeRepository(target.session_factory).list_chunks(
                report.document_id
            ) == await repo.list_chunks(report.document_id)
            with pytest.raises(MigrationError, match="same_source_and_target"):
                await migrate_guides(source_url, source_url)

            old = _pending()
            async with source.engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE guide_conversations "
                        "(id UUID, owner_id UUID, revision INTEGER, payload JSONB)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO guide_conversations "
                        "VALUES (:id, :owner, :revision, CAST(:payload AS jsonb))"
                    ),
                    {
                        "id": old.id,
                        "owner": old.owner_id,
                        "revision": old.revision,
                        "payload": json.dumps(conversation_to_data(old)),
                    },
                )
            migrated = await read_postgres(source_url, old.owner_id)
            assert len(migrated) == 1 and migrated[0].turns[-1].status == "interrupted"
            assert not await read_postgres(source_url, uuid4())
            async with source.engine.connect() as connection:
                assert await connection.scalar(select(GuideDocumentRecord.id)) == report.document_id
        finally:
            await target.dispose()
            await source.dispose()

    asyncio.run(run())
