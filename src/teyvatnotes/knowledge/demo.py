"""显式演示范围：临时内存中的虚构资料，永远不发布到真实知识库。"""

import asyncio
from pathlib import Path

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.knowledge.snapshots import load_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository

PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def create_demo_repository() -> InMemoryKnowledgeRepository:
    """顺序加载两份长期教学样本；不接触 DATABASE_URL、模型凭据或外部网站。"""
    repository = InMemoryKnowledgeRepository()
    service = GuideIngestionService(repository)
    for name in ("retrieval_character.json", "retrieval_quest.json"):
        snapshot = await asyncio.to_thread(
            load_snapshot, PROJECT_ROOT / "samples" / "guides" / name
        )
        await service.import_snapshot(snapshot)
    return repository
