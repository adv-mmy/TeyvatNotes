"""真实 PostgreSQL 隔离验证；只创建随机 schema，退出不删除证据。"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_guide_ingestion import CountingParser, article_snapshot

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.database import Base, Database, normalize_database_url
from teyvatnotes.database.knowledge import (
    GuideChunkRecord,
    GuideDocumentRecord,
    GuideStepRecord,
)
from teyvatnotes.domain import Task, TaskType
from teyvatnotes.domain.guides import (
    DocumentStatus,
    GuideChunk,
    GuideImportError,
    IngestionCheckpoint,
    StepName,
)
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories import PostgresTaskRepository
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

pytestmark = pytest.mark.postgres
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def migrate(connection: Connection, revision: str = "head") -> None:
    configuration = Config(PROJECT_ROOT / "alembic.ini")
    configuration.attributes["connection"] = connection
    command.upgrade(configuration, revision)


def connect(schema: str) -> Database:
    url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("未设置数据库 URL；真实测试不得用内存仓储替代")
    engine = create_async_engine(
        normalize_database_url(url),
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    return Database(engine, async_sessionmaker(engine, expire_on_commit=False))


@asynccontextmanager
async def isolated_database(revision: str = "head") -> AsyncIterator[tuple[Database, str]]:
    schema = f"guide_week1_{uuid4().hex}"
    database = connect(schema)
    try:
        async with database.engine.begin() as connection:
            # 名称只来自固定前缀与 UUID.hex，没有来自请求的 SQL 标识符。
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(lambda sync_connection: migrate(sync_connection, revision))
        yield database, schema
    finally:
        await database.dispose()


def test_upgrade_preserves_task_and_metadata_matches() -> None:
    async def scenario() -> None:
        async with isolated_database("20260825_0002") as (database, _):
            task = Task(task_type=TaskType.SUM_NUMBERS, payload={"numbers": [1, 2]})
            await PostgresTaskRepository(database.session_factory).add(task)
            async with database.engine.begin() as connection:
                await connection.run_sync(migrate)
                differences = await connection.run_sync(
                    lambda sync_connection: compare_metadata(
                        MigrationContext.configure(sync_connection, opts={"compare_type": True}),
                        Base.metadata,
                    )
                )
            assert differences == []
            saved = await PostgresTaskRepository(database.session_factory).get(task.id)
            assert saved is not None and saved.payload == {"numbers": [1, 2]}

    asyncio.run(scenario())


def test_concurrent_imports_across_engines_are_idempotent() -> None:
    async def scenario() -> None:
        async with isolated_database() as (first, schema):
            second = connect(schema)
            try:
                services = [
                    GuideIngestionService(PostgresKnowledgeRepository(db.session_factory))
                    for db in (first, second)
                ]
                snapshot = parse_snapshot(article_snapshot())
                reports = await asyncio.gather(
                    *(services[index % 2].import_snapshot(snapshot) for index in range(6))
                )
                assert len({report.document_id for report in reports}) == 1
                async with first.session_factory() as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(GuideDocumentRecord))
                        == 1
                    )
                    assert (
                        await session.scalar(select(func.count()).select_from(GuideStepRecord)) == 3
                    )
                    assert (
                        await session.scalar(select(func.count()).select_from(GuideChunkRecord))
                        == 3
                    )
                published = await services[0].import_snapshot(snapshot, publish=True)
                assert published.status is DocumentStatus.PUBLISHED
            finally:
                await second.dispose()

    asyncio.run(scenario())


def test_chunk_transaction_rolls_back_and_another_engine_resumes_parse() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, schema):

            def interrupt_step_write(
                connection: Connection,
                cursor: object,
                statement: str,
                parameters: object,
                context: object,
                many: bool,
            ) -> None:
                if statement.startswith("INSERT INTO guide_ingestion_steps"):
                    raise RuntimeError("controlled-transaction-failure")

            class InterruptedRepository(PostgresKnowledgeRepository):
                async def commit_checkpoint(
                    self, checkpoint: IngestionCheckpoint, *, chunks: tuple[GuideChunk, ...] = ()
                ) -> IngestionCheckpoint:
                    if checkpoint.name is not StepName.CHUNK:
                        return await super().commit_checkpoint(checkpoint, chunks=chunks)
                    event.listen(
                        database.engine.sync_engine, "before_cursor_execute", interrupt_step_write
                    )
                    try:
                        return await super().commit_checkpoint(checkpoint, chunks=chunks)
                    finally:
                        event.remove(
                            database.engine.sync_engine,
                            "before_cursor_execute",
                            interrupt_step_write,
                        )

            parser = CountingParser()
            service = GuideIngestionService(
                InterruptedRepository(database.session_factory),
                parser=parser,
                parser_version="test-v1",
            )
            snapshot = parse_snapshot(article_snapshot())
            with pytest.raises(RuntimeError, match="controlled-transaction-failure"):
                await service.import_snapshot(snapshot)
            async with database.session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(GuideChunkRecord)) == 0
                assert await session.scalar(select(func.count()).select_from(GuideStepRecord)) == 1
            restarted = connect(schema)
            try:
                other_parser = CountingParser()
                report = await GuideIngestionService(
                    PostgresKnowledgeRepository(restarted.session_factory),
                    parser=other_parser,
                    parser_version="test-v1",
                ).import_snapshot(snapshot)
                assert other_parser.calls == 0
                assert report.reused_steps == (StepName.PARSE,)
                assert report.chunk_count == 3
            finally:
                await restarted.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("nature", ["released", "preview"])
def test_postgres_publishes_and_retrieves_both_content_natures(nature: str) -> None:
    from teyvatnotes.domain.guides import GuideKind
    from teyvatnotes.domain.knowledge import EvidenceScope, KeywordQuery

    async def scenario() -> None:
        async with isolated_database() as (database, _):
            repository = PostgresKnowledgeRepository(database.session_factory)
            report = await GuideIngestionService(repository).import_snapshot(
                parse_snapshot(article_snapshot(entities=["示例任务"], content_nature=nature)),
                publish=True,
            )
            scope = EvidenceScope("test-version", GuideKind.QUEST, "示例任务")
            hits = await repository.search_knowledge(KeywordQuery(scope, ("第二步",)))
            assert len(hits) == 1 and hits[0].document.id == report.document_id
            assert hits[0].document.snapshot["content_nature"] == nature
            assert (await repository.read_evidence(hits[0].chunk.id, scope)).hit.chunk == hits[
                0
            ].chunk

    asyncio.run(scenario())


def test_postgres_publication_blocks_fixture_and_keeps_draft() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, _):
            repository = PostgresKnowledgeRepository(database.session_factory)
            service = GuideIngestionService(repository)
            snapshot = parse_snapshot(article_snapshot(is_fixture=True, game_version=None))
            report = await service.import_snapshot(snapshot)
            with pytest.raises(GuideImportError, match="publication_blocked"):
                await service.import_snapshot(snapshot, publish=True)
            assert (
                await repository.get_document(report.document_id)
            ).status is DocumentStatus.DRAFT

    asyncio.run(scenario())
