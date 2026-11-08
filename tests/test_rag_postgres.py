"""只创建随机 schema 的真实 RAG 验证：迁移保留证据，向量并发/重启与整批原子性。"""

import asyncio
from dataclasses import replace

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_guide_postgres import connect, isolated_database, migrate
from test_knowledge_retrieval import scope, searchable_snapshot
from test_rag_repository import SEMANTIC, vector

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.database import Base, GuideChunkVectorRecord
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.rag import content_digest, embedding_text
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository
from teyvatnotes.repositories.rag_memory import InMemoryRagRepository
from teyvatnotes.repositories.rag_postgres import PostgresRagRepository

pytestmark = pytest.mark.postgres


def test_rag_additive_upgrade_preserves_knowledge_and_metadata_matches() -> None:
    async def scenario() -> None:
        async with isolated_database("20261003_0003") as (database, _):
            knowledge = PostgresKnowledgeRepository(database.session_factory)
            report = await GuideIngestionService(knowledge).import_snapshot(
                parse_snapshot(searchable_snapshot()), publish=True
            )
            chunks = await knowledge.list_chunks(report.document_id)
            async with database.engine.begin() as connection:
                await connection.run_sync(migrate)
                differences = await connection.run_sync(
                    lambda sync_connection: compare_metadata(
                        MigrationContext.configure(sync_connection, opts={"compare_type": True}),
                        Base.metadata,
                    )
                )
            assert differences == []
            assert await knowledge.list_chunks(report.document_id) == chunks
            rag = PostgresRagRepository(database.session_factory)
            assert tuple(hit.chunk for hit in await rag.list_candidates(scope())) == chunks
            await rag.save_vectors((vector(chunks[0]),))
            assert await rag.get_vectors(SEMANTIC, (chunks[0].id,)) == (vector(chunks[0]),)

    asyncio.run(scenario())


def test_sql_candidates_match_memory_latest_conditions_demo_and_budget() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, _):
            memory_knowledge = InMemoryKnowledgeRepository()
            postgres_knowledge = PostgresKnowledgeRepository(database.session_factory)
            memory = InMemoryRagRepository(memory_knowledge)
            postgres = PostgresRagRepository(database.session_factory)
            for index, changes in enumerate(
                ({}, {"conditions": {"前置": "完成"}}, {"is_fixture": True})
            ):
                identity = str(3100000000 + index)
                snapshot = parse_snapshot(
                    searchable_snapshot(
                        native_id=identity,
                        url=f"https://www.miyoushe.com/ys/article/{identity}",
                        **changes,
                    )
                )
                for knowledge in (memory_knowledge, postgres_knowledge):
                    await GuideIngestionService(knowledge).import_snapshot(
                        snapshot, publish=index != 2
                    )
            for query_scope in (
                scope(),
                scope(conditions=(("前置", "完成"), ("额外", "允许"))),
                scope(conditions=(("前置", "未完成"),)),
                scope(demo_only=True),
                scope(game_version="other"),
                scope(entity="other"),
            ):
                actual = await postgres.list_candidates(query_scope)
                expected = await memory.list_candidates(query_scope)
                assert [(hit.document.id, hit.chunk, hit.score) for hit in actual] == [
                    (hit.document.id, hit.chunk, hit.score) for hit in expected
                ]
            changed = parse_snapshot(
                searchable_snapshot(
                    native_id="3100000000",
                    url="https://www.miyoushe.com/ys/article/3100000000",
                    conditions={"新前置": "完成"},
                    article_blocks=[{"kind": "paragraph", "text": "新版证据。"}],
                )
            )
            await GuideIngestionService(postgres_knowledge).import_snapshot(changed)
            assert await postgres.list_candidates(scope())
            await GuideIngestionService(postgres_knowledge).import_snapshot(changed, publish=True)
            assert await postgres.list_candidates(scope()) == ()
            conditioned = scope(conditions=(("新前置", "完成"),))
            assert len(await postgres.list_candidates(conditioned)) == 1
            with pytest.raises(GuideImportError, match="rag_candidate_budget_exceeded"):
                await postgres.list_candidates(scope(demo_only=True), max_chunks=1)

    asyncio.run(scenario())


def test_sql_vectors_are_idempotent_profile_isolated_and_survive_new_engine() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, schema):
            knowledge = PostgresKnowledgeRepository(database.session_factory)
            report = await GuideIngestionService(knowledge).import_snapshot(
                parse_snapshot(searchable_snapshot()), publish=True
            )
            chunks = await knowledge.list_chunks(report.document_id)
            rag = PostgresRagRepository(database.session_factory)
            entries = tuple(vector(chunk) for chunk in chunks)
            await rag.save_vectors(entries)
            await rag.save_vectors(entries)
            assert await rag.get_vectors(SEMANTIC, ()) == ()
            await rag.save_vectors(())
            restarted = connect(schema)
            try:
                reader = PostgresRagRepository(restarted.session_factory)
                assert await reader.get_vectors(
                    SEMANTIC, tuple(chunk.id for chunk in reversed(chunks))
                ) == (tuple(reversed(entries)))
                assert (
                    await reader.get_vectors(replace(SEMANTIC, revision="rev-2"), (chunks[0].id,))
                    == ()
                )
                assert (await reader.get_vectors(SEMANTIC, (chunks[0].id,)))[0].values == (3.0, 4.0)
            finally:
                await restarted.dispose()

    asyncio.run(scenario())


def test_sql_conflict_or_invalid_hash_rolls_back_whole_batch() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, _):
            knowledge = PostgresKnowledgeRepository(database.session_factory)
            report = await GuideIngestionService(knowledge).import_snapshot(
                parse_snapshot(searchable_snapshot()), publish=True
            )
            chunks = await knowledge.list_chunks(report.document_id)
            rag = PostgresRagRepository(database.session_factory)
            original = vector(chunks[0])
            await rag.save_vectors((original,))
            for failure, code in (
                (replace(original, values=(4.0, 3.0)), "vector_conflict"),
                (replace(original, content_hash="0" * 64), "vector_content_mismatch"),
            ):
                with pytest.raises(GuideImportError, match=code):
                    await rag.save_vectors((vector(chunks[1]), failure))
                assert await rag.get_vectors(SEMANTIC, tuple(chunk.id for chunk in chunks)) == (
                    original,
                )

    asyncio.run(scenario())


def test_sql_concurrent_engines_have_one_vector_winner_and_no_overwrite() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, schema):
            knowledge = PostgresKnowledgeRepository(database.session_factory)
            report = await GuideIngestionService(knowledge).import_snapshot(
                parse_snapshot(searchable_snapshot()), publish=True
            )
            chunk = (await knowledge.list_chunks(report.document_id))[0]
            original = vector(chunk)
            other = replace(original, values=(1.0, 1.0))
            second = connect(schema)
            try:
                first_rag = PostgresRagRepository(database.session_factory)
                second_rag = PostgresRagRepository(second.session_factory)
                results = await asyncio.gather(
                    first_rag.save_vectors((original,)),
                    second_rag.save_vectors((other,)),
                    return_exceptions=True,
                )
                assert sum(result is None for result in results) == 1
                failure = next(result for result in results if isinstance(result, GuideImportError))
                assert failure.code == "vector_conflict"
                assert (await first_rag.get_vectors(SEMANTIC, (chunk.id,)))[0] in (original, other)
            finally:
                await second.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("values", [[0.0, 0.0], [1.0], [1.0, "private-text"]])
def test_sql_constraints_reject_zero_dimension_mismatch_or_non_numeric_values(
    values: list[object],
) -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, _):
            knowledge = PostgresKnowledgeRepository(database.session_factory)
            report = await GuideIngestionService(knowledge).import_snapshot(
                parse_snapshot(searchable_snapshot()), publish=True
            )
            chunk = (await knowledge.list_chunks(report.document_id))[0]
            with pytest.raises(IntegrityError):
                async with database.session_factory() as session, session.begin():
                    session.add(
                        GuideChunkVectorRecord(
                            profile_id=SEMANTIC.identity,
                            chunk_id=chunk.id,
                            provider=SEMANTIC.provider,
                            model=SEMANTIC.model,
                            revision=SEMANTIC.revision,
                            dimensions=SEMANTIC.dimensions,
                            demo_only=False,
                            semantic=True,
                            content_hash=content_digest(embedding_text(chunk)),
                            values=values,
                        )
                    )
                    await session.flush()
            async with database.session_factory() as session:
                assert (await session.scalars(select(GuideChunkVectorRecord))).all() == []

    asyncio.run(scenario())
