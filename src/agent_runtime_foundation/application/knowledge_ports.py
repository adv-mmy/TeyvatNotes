"""知识存储与快照解析端口，不依赖具体数据库或 Pydantic。"""

from typing import Protocol
from uuid import UUID

from agent_runtime_foundation.domain.guides import (
    EvidenceBlock,
    GuideChunk,
    GuideDocument,
    GuideSnapshot,
    IngestionCheckpoint,
    StepName,
)


class SnapshotParser(Protocol):
    """可替换且需要显式处理版本的确定性解析协议。"""

    def parse(self, snapshot: GuideSnapshot) -> tuple[EvidenceBlock, ...]:
        """从已校验的快照提取有序证据块。"""


class KnowledgeRepository(Protocol):
    """版本、检查点和发布规则的异步存储契约。"""

    async def register(self, document: GuideDocument) -> GuideDocument:
        """按不可变版本 ID 幂等注册来源和文档。"""

    async def get_document(self, document_id: UUID) -> GuideDocument:
        """查询一个文档版本，不修改状态。"""

    async def get_checkpoint(self, document_id: UUID, name: StepName) -> IngestionCheckpoint | None:
        """读取阶段结果，供恢复时验证输入哈希。"""

    async def commit_checkpoint(
        self, checkpoint: IngestionCheckpoint, *, chunks: tuple[GuideChunk, ...] = ()
    ) -> IngestionCheckpoint:
        """在一个临界区/事务中保存检查点与切块，已完成结果不可被覆盖。"""

    async def list_chunks(self, document_id: UUID) -> tuple[GuideChunk, ...]:
        """按 ordinal 返回具有原文位置的切块。"""

    async def publish(self, document_id: UUID) -> GuideDocument:
        """检查阶段和阻断项后原子发布；不能通过改状态绕过检查。"""
