"""第二阶段第一周：离线资料解析、检查点与发布边界。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from scripts.guide_import_demo import run_guide_import_demo
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.domain.guides import (
    DocumentStatus,
    EvidenceBlock,
    GuideChunk,
    GuideImportError,
    GuideSnapshot,
    IngestionCheckpoint,
    StepName,
    StepStatus,
)
from teyvatnotes.knowledge.parsing import OfflineSnapshotParser
from teyvatnotes.knowledge.snapshots import load_snapshot, parse_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository


def article_snapshot(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "platform": "miyoushe",
        "native_id": "1000000000",
        "url": "https://www.miyoushe.com/ys/article/1000000000",
        "title": "测试用任务资料，不代表真实游戏内容",
        "author": "测试作者",
        "guide_kind": "quest",
        "game_version": "test-version",
        "complete": True,
        "is_fixture": False,
        "article_blocks": [
            {"kind": "heading", "text": "前置条件"},
            {"kind": "paragraph", "text": "先完成示例前置条件。"},
            {"kind": "heading", "text": "步骤"},
            {"kind": "paragraph", "text": "第一步操作示例。"},
            {"kind": "paragraph", "text": "第二步检查完成标志。"},
        ],
    }
    value.update(changes)
    return value


def test_import_reuses_checkpoints_and_keeps_source_order() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        service = GuideIngestionService(repository)
        source = parse_snapshot(article_snapshot())
        first = await service.import_snapshot(source)
        second = await service.import_snapshot(source)
        assert first.document_id == second.document_id
        assert first.status is DocumentStatus.DRAFT
        assert second.reused_steps == (StepName.PARSE, StepName.CHUNK, StepName.VALIDATE)
        chunks = await repository.list_chunks(first.document_id)
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
        assert chunks[0].section == "前置条件"
        assert "第一步" in chunks[1].text
        assert chunks[1].locators[0]["block_index"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("changes", "issue"),
    [
        ({"game_version": None}, "version_unknown"),
        ({"is_fixture": True}, "fixture_source"),
        ({"complete": False}, "source_incomplete"),
        ({"content_nature": "preview", "complete": False}, "source_incomplete"),
        (
            {
                "content_nature": "preview",
                "article_blocks": [
                    {
                        "kind": "image",
                        "text": "待核对",
                        "image_url": "https://example.invalid/a.png",
                        "requires_visual": True,
                    }
                ],
            },
            "visual_evidence_missing",
        ),
    ],
)
def test_incomplete_or_synthetic_sources_cannot_publish(
    changes: dict[str, object], issue: str
) -> None:
    async def scenario() -> None:
        service = GuideIngestionService(InMemoryKnowledgeRepository())
        source = parse_snapshot(article_snapshot(**changes))
        report = await service.import_snapshot(source)
        assert issue in report.blocking_issues
        with pytest.raises(GuideImportError, match="publication_blocked"):
            await service.import_snapshot(source, publish=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("nature", ["released", "preview"])
def test_content_nature_is_metadata_not_a_publication_gate(nature: str) -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        report = await GuideIngestionService(repository).import_snapshot(
            parse_snapshot(article_snapshot(content_nature=nature)), publish=True
        )
        assert report.status is DocumentStatus.PUBLISHED
        assert report.blocking_issues == ()
        assert "preview_content" not in report.warnings
        assert (await repository.get_document(report.document_id)).snapshot[
            "content_nature"
        ] == nature

    asyncio.run(scenario())


def test_new_policy_does_not_reuse_legacy_preview_validation() -> None:
    class LegacyRepository(InMemoryKnowledgeRepository):
        async def commit_checkpoint(
            self, checkpoint: IngestionCheckpoint, *, chunks: tuple[GuideChunk, ...] = ()
        ) -> IngestionCheckpoint:
            document = await self.get_document(checkpoint.document_id)
            if (
                document.processing_version.startswith("offline-v1:")
                and checkpoint.name is StepName.VALIDATE
            ):
                checkpoint = replace(
                    checkpoint, output={"blocking_issues": ["preview_content"], "warnings": []}
                )
            return await super().commit_checkpoint(checkpoint, chunks=chunks)

    async def scenario() -> None:
        repository = LegacyRepository()
        snapshot = parse_snapshot(article_snapshot(content_nature="preview"))
        legacy = await GuideIngestionService(
            repository, parser_version="offline-v1"
        ).import_snapshot(snapshot)
        current = await GuideIngestionService(repository).import_snapshot(snapshot, publish=True)
        assert legacy.blocking_issues == ("preview_content",)
        assert current.document_id != legacy.document_id
        assert current.status is DocumentStatus.PUBLISHED and current.reused_steps == ()
        old = await repository.get_checkpoint(legacy.document_id, StepName.VALIDATE)
        assert old is not None and old.output["blocking_issues"] == ["preview_content"]
        assert await repository.list_chunks(legacy.document_id)

    asyncio.run(scenario())


def test_changed_content_creates_new_version_and_old_evidence_survives() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        service = GuideIngestionService(repository)
        original = article_snapshot()
        first = await service.import_snapshot(parse_snapshot(original), publish=True)
        changed = deepcopy(original)
        changed["title"] = "修改后的测试标题"
        second = await service.import_snapshot(parse_snapshot(changed))
        assert first.document_id != second.document_id
        assert (await repository.get_document(first.document_id)).status is DocumentStatus.PUBLISHED
        assert await repository.list_chunks(first.document_id)

    asyncio.run(scenario())


def test_concurrent_duplicate_imports_have_one_document_and_stable_chunks() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        service = GuideIngestionService(repository)
        source = parse_snapshot(article_snapshot())
        reports = await asyncio.gather(*(service.import_snapshot(source) for _ in range(8)))
        assert len({report.document_id for report in reports}) == 1
        chunks = await repository.list_chunks(reports[0].document_id)
        assert len({chunk.id for chunk in chunks}) == len(chunks)

    asyncio.run(scenario())


class CountingParser:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once

    def parse(self, snapshot: GuideSnapshot) -> tuple[EvidenceBlock, ...]:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise RuntimeError("SECRET-EXCEPTION-MUST-NOT-BE-EXPOSED")
        return OfflineSnapshotParser().parse(snapshot)


def test_failed_parse_is_recorded_and_retry_finishes_without_exposing_exception() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        parser = CountingParser(fail_once=True)
        service = GuideIngestionService(repository, parser=parser, parser_version="test-v1")
        source = parse_snapshot(article_snapshot())
        with pytest.raises(GuideImportError, match="^step_failed$"):
            await service.import_snapshot(source)
        completed = await service.import_snapshot(source)
        checkpoint = await repository.get_checkpoint(completed.document_id, StepName.PARSE)
        assert checkpoint is not None and checkpoint.status is StepStatus.COMPLETED
        assert checkpoint.error_code is None
        assert parser.calls == 2
        await service.import_snapshot(source)
        assert parser.calls == 2

    asyncio.run(scenario())


def test_parser_version_and_chunk_size_are_part_of_document_identity() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        snapshot = parse_snapshot(article_snapshot())
        parser = CountingParser()
        with pytest.raises(ValueError, match="parser_version"):
            GuideIngestionService(repository, parser=parser)
        with pytest.raises(ValueError, match="parser_version"):
            GuideIngestionService(repository, parser_version="")
        first = await GuideIngestionService(
            repository, parser=parser, parser_version="test-v1"
        ).import_snapshot(snapshot)
        second = await GuideIngestionService(
            repository, parser=parser, parser_version="test-v2"
        ).import_snapshot(snapshot)
        third = await GuideIngestionService(
            repository, parser=parser, parser_version="test-v2", max_chunk_chars=100
        ).import_snapshot(snapshot)
        assert len({first.document_id, second.document_id, third.document_id}) == 3
        assert parser.calls == 3

    asyncio.run(scenario())


def test_nested_repository_results_are_isolated_and_completed_steps_are_immutable() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        report = await GuideIngestionService(repository).import_snapshot(
            parse_snapshot(article_snapshot())
        )
        document = await repository.get_document(report.document_id)
        document.snapshot["title"] = "不应写回仓储"
        assert (await repository.get_document(report.document_id)).snapshot[
            "title"
        ] != "不应写回仓储"
        chunks = await repository.list_chunks(report.document_id)
        chunks[0].locators[0]["block_index"] = 999
        assert (await repository.list_chunks(report.document_id))[0].locators[0]["block_index"] == 1
        step = await repository.get_checkpoint(report.document_id, StepName.PARSE)
        assert step is not None
        failed = replace(step, status=StepStatus.FAILED, output={}, error_code="step_failed")
        assert (await repository.commit_checkpoint(failed)).status is StepStatus.COMPLETED
        step.output.clear()
        assert (await repository.get_checkpoint(report.document_id, StepName.PARSE)).output

    asyncio.run(scenario())


def test_visual_gaps_are_blocking_and_long_paragraphs_keep_original_offsets() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        service = GuideIngestionService(repository, max_chunk_chars=100)
        value = article_snapshot(
            article_blocks=[
                {"kind": "heading", "text": "步骤"},
                {"kind": "paragraph", "text": "长段落" * 100},
                {
                    "kind": "image",
                    "text": "关键画面未解析",
                    "image_url": "https://example.invalid/x.png",
                    "requires_visual": True,
                },
            ]
        )
        report = await service.import_snapshot(parse_snapshot(value))
        assert "visual_evidence_missing" in report.blocking_issues
        assert "images_reference_only" in report.warnings
        chunks = await repository.list_chunks(report.document_id)
        assert all(len(chunk.text) <= 100 for chunk in chunks)
        assert chunks[1].locators[0]["start_char"] == 100
        assert chunks[1].section == "步骤"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"unexpected": "SECRET"},
        {"published_at": "2026-10-03T12:00:00"},
        {"url": "https://evil.invalid/ys/article/1000000000"},
        {"url": "https://user:SECRET@www.miyoushe.com/ys/article/1000000000"},
        {"url": "https://www.miyoushe.com/ys/article/999"},
        {"complete": "true"},
    ],
)
def test_snapshot_rejects_unknown_fields_credentials_and_bad_metadata(
    changes: dict[str, object],
) -> None:
    with pytest.raises(GuideImportError, match="^invalid_snapshot$"):
        parse_snapshot(article_snapshot(**changes))


def test_subtitles_keep_part_and_timestamps_and_reject_misalignment() -> None:
    root = Path(__file__).resolve().parents[1]
    snapshot = load_snapshot(root / "samples/guides/bilibili_character.json")
    blocks = OfflineSnapshotParser().parse(snapshot)
    assert blocks[1].locator == {"part_id": 1, "start": 8.0, "end": 18.0, "segment_index": 1}
    bad = deepcopy(snapshot.data)
    bad["part_id"] = 2
    with pytest.raises(GuideImportError, match="invalid_snapshot"):
        parse_snapshot(bad)
    bad["url"] = "https://www.bilibili.com/video/BV1DEMO00001?p=2&tracking=SECRET"
    other = parse_snapshot(bad)
    assert other.source.id != snapshot.source.id
    assert "tracking" not in other.source.canonical_url
    bad["segments"] = [{"start": 8, "end": 4, "text": "错误时间"}]
    with pytest.raises(GuideImportError, match="invalid_snapshot"):
        parse_snapshot(bad)


def test_offline_demo_checks_reuse_and_never_publishes_fixtures() -> None:
    result = asyncio.run(run_guide_import_demo())
    assert result["external_requests"] == 0
    assert len(result["documents"]) == 2


def test_cancelled_import_propagates_and_resumes_completed_parse() -> None:
    class PausingRepository(InMemoryKnowledgeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.paused = asyncio.Event()
            self.pause = True

        async def get_checkpoint(
            self, document_id: UUID, name: StepName
        ) -> IngestionCheckpoint | None:
            if self.pause and name is StepName.CHUNK:
                self.paused.set()
                await asyncio.Event().wait()
            return await super().get_checkpoint(document_id, name)

    async def scenario() -> None:
        repository = PausingRepository()
        service = GuideIngestionService(repository)
        snapshot = parse_snapshot(article_snapshot())
        running = asyncio.create_task(service.import_snapshot(snapshot))
        await repository.paused.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        repository.pause = False
        report = await service.import_snapshot(snapshot)
        assert report.reused_steps == (StepName.PARSE,)

    asyncio.run(scenario())


def test_register_always_starts_as_draft_and_cannot_bypass_publication() -> None:
    async def scenario() -> None:
        original = InMemoryKnowledgeRepository()
        report = await GuideIngestionService(original).import_snapshot(
            parse_snapshot(article_snapshot(is_fixture=True))
        )
        document = await original.get_document(report.document_id)
        repository = InMemoryKnowledgeRepository()
        forced = replace(document, status=DocumentStatus.PUBLISHED)
        assert (await repository.register(forced)).status is DocumentStatus.DRAFT
        with pytest.raises(GuideImportError, match="publication_blocked"):
            await repository.publish(document.id)

    asyncio.run(scenario())
