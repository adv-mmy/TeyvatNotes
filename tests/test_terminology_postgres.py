"""专名校对真实持久化；仅用随机schema，不调用模型，不删除历史证据。"""

import asyncio

import pytest
from test_guide_ingestion import article_snapshot
from test_guide_postgres import connect, isolated_database

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.domain.guides import GuideKind
from teyvatnotes.domain.knowledge import EvidenceScope, KeywordQuery
from teyvatnotes.domain.terminology import TerminologyCatalog
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.knowledge.terminology import load_catalog
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

pytestmark = pytest.mark.postgres


def test_wiki_catalog_corrections_and_old_evidence_survive_new_engine() -> None:
    async def scenario() -> None:
        async with isolated_database() as (first, schema):
            writer = PostgresKnowledgeRepository(first.session_factory)
            source = parse_snapshot(
                article_snapshot(
                    guide_kind="character",
                    entities=["奥萝塔"],
                    article_blocks=[
                        {"kind": "heading", "text": "奥萝塔：配队"},
                        {"kind": "paragraph", "text": "薇斯纳，奥萝塔。"},
                    ],
                )
            )
            # 模拟尚无词表时的旧版本；只在随机测试schema构造发布身份。
            old_catalog = TerminologyCatalog("before-wiki-v1", ())
            old = await GuideIngestionService(
                writer, terminology_catalog=old_catalog
            ).import_snapshot(source, publish=True)
            current_catalog = load_catalog()
            service = GuideIngestionService(writer, terminology_catalog=current_catalog)
            current = await service.import_snapshot(source, publish=True)
            assert old.document_id != current.document_id

            second = connect(schema)
            try:
                reader = PostgresKnowledgeRepository(second.session_factory)
                document = await reader.get_document(current.document_id)
                audit = document.snapshot["_terminology"]
                assert audit["catalog_hash"] == current_catalog.content_hash
                assert len(audit["catalog"]["terms"]) == 444
                assert audit["raw_entities"] == ["奥萝塔"]
                assert document.snapshot["entities"] == ["奥黛塔"]
                assert document.snapshot["article_blocks"][1]["text"] == "薇斯纳，奥萝塔。"
                chunks = await reader.list_chunks(current.document_id)
                assert chunks[0].text == "薇斯纳，奥黛塔。"
                assert chunks[0].section == "奥黛塔：配队"
                location = chunks[0].locators[0]["terminology"]
                assert location["source_text"] == "薇斯纳，奥萝塔。"
                assert location["offset_basis"] == "normalized_text"
                scope = EvidenceScope("test-version", GuideKind.CHARACTER, "奥黛塔")
                hits = await reader.search_knowledge(KeywordQuery(scope, ("奥黛塔",)))
                assert hits[0].chunk == chunks[0]
                assert (await reader.read_evidence(chunks[0].id, scope)).chunks == chunks
                old_chunks = await reader.list_chunks(old.document_id)
                assert old_chunks[0].text == "薇斯纳，奥萝塔。"
                assert (await reader.get_document(old.document_id)).snapshot["_terminology"][
                    "catalog_hash"
                ] == old_catalog.content_hash
                reused = await GuideIngestionService(
                    reader, terminology_catalog=current_catalog
                ).import_snapshot(source)
                assert reused.document_id == current.document_id and len(reused.reused_steps) == 3
            finally:
                await second.dispose()

    asyncio.run(scenario())
