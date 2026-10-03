"""知识只读端口，与第一周导入写端口分开。"""

from typing import Protocol
from uuid import UUID

from agent_runtime_foundation.domain.knowledge import (
    EvidenceHit,
    EvidenceScope,
    EvidenceWindow,
    KeywordQuery,
)


class KnowledgeReader(Protocol):
    """检索和按已存证据 ID 回读，不接受任意来源 URL。"""

    async def search_knowledge(self, query: KeywordQuery) -> tuple[EvidenceHit, ...]:
        """每来源/版本/类型只检索最新已发布版本，再过滤实体、条件并稳定排序。"""

    async def read_evidence(
        self, chunk_id: UUID, scope: EvidenceScope, *, max_chunks: int = 20
    ) -> EvidenceWindow:
        """回读精确历史版本及上下文，再检查可见性；无权限范围返回同一未找到错误。"""
