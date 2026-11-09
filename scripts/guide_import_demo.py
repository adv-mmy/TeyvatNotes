"""无网络、无模型、无数据库的两类资料导入与重复恢复演示。"""

import asyncio
from pathlib import Path

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.domain import JsonValue
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.knowledge.cli import report_to_json
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.knowledge.snapshots import load_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def run_guide_import_demo() -> dict[str, JsonValue]:
    repository = InMemoryKnowledgeRepository()
    service = GuideIngestionService(repository)
    results: list[JsonValue] = []
    for filename in ("bilibili_character.json", "miyoushe_quest.json"):
        snapshot = await asyncio.to_thread(
            load_snapshot, PROJECT_ROOT / "samples" / "guides" / filename
        )
        first = await service.import_snapshot(snapshot)
        second = await service.import_snapshot(snapshot)
        assert first.document_id == second.document_id
        assert len(second.reused_steps) == 3
        try:
            await service.import_snapshot(snapshot, publish=True)
        except GuideImportError as exc:
            assert exc.code == "publication_blocked"
        else:
            raise AssertionError("教学样本不应发布")
        results.append({**report_to_json(second), "publication_blocked": True})
    return {"storage": "memory", "external_requests": 0, "documents": results}


def main() -> None:
    emit_json(asyncio.run(run_guide_import_demo()))


if __name__ == "__main__":
    main()
