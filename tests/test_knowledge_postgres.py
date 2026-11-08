"""真实 PostgreSQL 检索与内存契约一致；只创建隔离 schema，保留验证证据。"""

import asyncio

import pytest
from sqlalchemy import func, select
from test_guide_postgres import connect, isolated_database
from test_knowledge_retrieval import scope, searchable_snapshot

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.database.knowledge import GuideStepRecord
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.knowledge import KeywordQuery
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

pytestmark = pytest.mark.postgres


def test_sql_literal_search_conditions_order_and_read_match_memory() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, _):
            memory = InMemoryKnowledgeRepository()
            postgres = PostgresKnowledgeRepository(database.session_factory)
            for i, changes in enumerate(
                (
                    {},
                    {"conditions": {"前置任务": "已完成"}},
                    {"game_version": "other"},
                    {"is_fixture": True},
                )
            ):
                identity = str(2000000000 + i)
                value = searchable_snapshot(
                    native_id=identity,
                    url=f"https://www.miyoushe.com/ys/article/{identity}",
                    article_blocks=[
                        {"kind": "paragraph", "text": "前置说明。"},
                        {"kind": "paragraph", "text": "Build 使用 50%_ 成本。"},
                    ],
                    **changes,
                )
                for repository in (memory, postgres):
                    await GuideIngestionService(repository).import_snapshot(
                        parse_snapshot(value), publish=i != 3
                    )
            for conditions in ((), (("前置任务", "已完成"),), (("前置任务", "未完成"),)):
                query = KeywordQuery(scope(conditions=conditions), ("BUILD", "%_", "build"))
                expected = await memory.search_knowledge(query)
                actual = await postgres.search_knowledge(query)
                assert [(h.chunk.id, h.score) for h in actual] == [
                    (h.chunk.id, h.score) for h in expected
                ]
                for hit in actual:
                    window = await postgres.read_evidence(hit.chunk.id, query.scope)
                    assert (
                        window.chunks
                        == (await memory.read_evidence(hit.chunk.id, query.scope)).chunks
                    )
                    assert not window.context_truncated

    asyncio.run(scenario())


def test_sql_latest_version_draft_does_not_hide_old_and_historic_id_remains_readable() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, schema):
            writer = PostgresKnowledgeRepository(database.session_factory)
            service = GuideIngestionService(writer)
            await service.import_snapshot(parse_snapshot(searchable_snapshot()), publish=True)
            old = (await writer.search_knowledge(KeywordQuery(scope(), ("第二步",))))[0]
            changed = searchable_snapshot(
                article_blocks=[{"kind": "paragraph", "text": "新版操作。"}]
            )
            await service.import_snapshot(parse_snapshot(changed))
            assert (await writer.search_knowledge(KeywordQuery(scope(), ("第二步",))))[
                0
            ].chunk.id == old.chunk.id
            await service.import_snapshot(parse_snapshot(changed), publish=True)
            restarted = connect(schema)
            try:
                reader = PostgresKnowledgeRepository(restarted.session_factory)
                assert await reader.search_knowledge(KeywordQuery(scope(), ("第二步",))) == ()
                assert (
                    await reader.read_evidence(old.chunk.id, scope())
                ).hit.document.id == old.document.id
                with pytest.raises(GuideImportError, match="evidence_not_found"):
                    await reader.read_evidence(old.chunk.id, scope(game_version="wrong"))
                with pytest.raises(GuideImportError, match="evidence_not_found"):
                    await reader.read_evidence(old.chunk.id, scope(demo_only=True))
                async with database.session_factory() as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(GuideStepRecord)) == 6
                    )
            finally:
                await restarted.dispose()

    asyncio.run(scenario())


def test_sql_context_is_bounded_and_preserves_truncation_flag() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, _):
            repository = PostgresKnowledgeRepository(database.session_factory)
            await GuideIngestionService(repository).import_snapshot(
                parse_snapshot(
                    searchable_snapshot(
                        article_blocks=[
                            {"kind": "paragraph", "text": f"第{i}步。"} for i in range(25)
                        ]
                    )
                ),
                publish=True,
            )
            hit = (await repository.search_knowledge(KeywordQuery(scope(), ("第24步",))))[0]
            result = await repository.read_evidence(hit.chunk.id, scope(), max_chunks=3)
            assert [c.ordinal for c in result.chunks] == [0, 1, 2] and result.context_truncated

    asyncio.run(scenario())
