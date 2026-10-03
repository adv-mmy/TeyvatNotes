"""阶段结果持久化的离线导入用例，与任务重试和模型调用解耦。"""

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid5

from agent_runtime_foundation.application.knowledge_ports import KnowledgeRepository, SnapshotParser
from agent_runtime_foundation.domain.guides import (
    DocumentStatus,
    GuideChunk,
    GuideDocument,
    GuideImportError,
    GuideSnapshot,
    ImportReport,
    IngestionCheckpoint,
    StepName,
    StepStatus,
)
from agent_runtime_foundation.domain.task import JsonValue
from agent_runtime_foundation.knowledge.parsing import (
    OfflineSnapshotParser,
    blocks_from_json,
    blocks_to_json,
    chunk_blocks,
    json_list,
    json_object,
    json_text,
)

PROCESSING_VERSION = "offline-v1"


def fingerprint(value: JsonValue) -> str:
    """对排序且无非有限数字的 UTF-8 JSON 计算稳定 SHA-256。"""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validation_result(document: GuideDocument, chunk_count: int) -> dict[str, JsonValue]:
    """结构与缺口检查，不声称验证作者结论或游戏事实。"""

    issues: list[JsonValue] = []
    warnings: list[JsonValue] = []
    data = document.snapshot
    if data.get("complete") is not True:
        issues.append("source_incomplete")
    if not document.game_version:
        issues.append("version_unknown")
    if data.get("is_fixture") is not False:
        issues.append("fixture_source")
    if data.get("content_nature") != "released":
        issues.append("preview_content")
    if chunk_count == 0:
        issues.append("no_text_evidence")
    if data.get("requires_visual") is True:
        issues.append("visual_evidence_missing")
    for item in json_list(data.get("article_blocks", [])):
        block = json_object(item)
        if block.get("kind") == "image":
            if "images_reference_only" not in warnings:
                warnings.append("images_reference_only")
            if block.get("requires_visual") is True and "visual_evidence_missing" not in issues:
                issues.append("visual_evidence_missing")
    if data.get("published_at") is None:
        warnings.append("publication_time_unknown")
    return {"blocking_issues": issues, "warnings": warnings}


class GuideIngestionService:
    """按输入哈希和处理配置复用已完成阶段，不自动发布。"""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        parser: SnapshotParser | None = None,
        max_chunk_chars: int = 600,
        parser_version: str | None = None,
    ) -> None:
        if not 100 <= max_chunk_chars <= 2000:
            raise ValueError("max_chunk_chars 必须在 100 到 2000 之间")
        if parser is not None and parser_version is None:
            raise ValueError("自定义 parser 必须显式提供 parser_version")
        resolved_version = PROCESSING_VERSION if parser_version is None else parser_version
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", resolved_version) is None:
            raise ValueError("parser_version 必须是 1 到 40 个安全版本字符")
        self._parser_version = resolved_version
        self._repository = repository
        self._parser = parser or OfflineSnapshotParser()
        self._max_chars = max_chunk_chars

    async def import_snapshot(
        self, snapshot: GuideSnapshot, *, publish: bool = False
    ) -> ImportReport:
        # frozen dataclass 不冻结内部字典，先隔离调用方后续修改。
        snapshot = deepcopy(snapshot)
        content_hash = fingerprint(snapshot.data)
        version = f"{self._parser_version}:chars-{self._max_chars}"
        document = await self._repository.register(
            GuideDocument(
                uuid5(snapshot.source.id, f"{content_hash}:{version}"),
                snapshot.source,
                content_hash,
                version,
                snapshot.title,
                snapshot.author,
                snapshot.guide_kind,
                snapshot.game_version,
                deepcopy(snapshot.data),
                DocumentStatus.DRAFT,
                datetime.now(UTC),
            )
        )
        reused: list[StepName] = []
        previous: dict[str, JsonValue] = {}
        for name in StepName:
            input_hash = fingerprint(
                {
                    "content_hash": content_hash,
                    "version": version,
                    "step": name.value,
                    "previous": previous,
                }
            )
            cached = await self._repository.get_checkpoint(document.id, name)
            if cached is not None and cached.status is StepStatus.COMPLETED:
                if cached.input_hash != input_hash:
                    raise GuideImportError("checkpoint_conflict")
                previous = cached.output
                reused.append(name)
                continue
            chunks: tuple[GuideChunk, ...] = ()
            try:
                if name is StepName.PARSE:
                    output: dict[str, JsonValue] = {
                        "blocks": blocks_to_json(self._parser.parse(snapshot))
                    }
                elif name is StepName.CHUNK:
                    chunks = chunk_blocks(
                        document.id, blocks_from_json(previous["blocks"]), max_chars=self._max_chars
                    )
                    output = {"chunk_count": len(chunks)}
                else:
                    output = validation_result(
                        document, len(await self._repository.list_chunks(document.id))
                    )
            except Exception as exc:
                code = exc.code if isinstance(exc, GuideImportError) else "step_failed"
                await self._repository.commit_checkpoint(
                    IngestionCheckpoint(
                        document.id,
                        name,
                        StepStatus.FAILED,
                        input_hash,
                        {},
                        code,
                        datetime.now(UTC),
                    )
                )
                raise GuideImportError(code) from exc
            saved = await self._repository.commit_checkpoint(
                IngestionCheckpoint(
                    document.id,
                    name,
                    StepStatus.COMPLETED,
                    input_hash,
                    output,
                    None,
                    datetime.now(UTC),
                ),
                chunks=chunks,
            )
            if saved.input_hash != input_hash:
                raise GuideImportError("checkpoint_conflict")
            previous = saved.output
        if publish:
            document = await self._repository.publish(document.id)
        else:
            document = await self._repository.get_document(document.id)
        return ImportReport(
            document.id,
            document.status,
            len(await self._repository.list_chunks(document.id)),
            tuple(json_text(item) for item in json_list(previous["blocking_issues"])),
            tuple(json_text(item) for item in json_list(previous["warnings"])),
            tuple(reused),
        )
