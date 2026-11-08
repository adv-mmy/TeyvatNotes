"""RAG 的供应商无关端口；不从 Responses 文本伪造语义 Embedding。"""

from typing import Protocol
from uuid import UUID

from teyvatnotes.domain.knowledge import EvidenceHit, EvidenceScope
from teyvatnotes.domain.rag import EmbeddingProfile, VectorEntry


class EmbeddingClient(Protocol):
    @property
    def profile(self) -> EmbeddingProfile: ...

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """调用方验证数量/维度/有限数；实现必须合作传播取消，不隐式重试。"""
        ...


class RagRepository(Protocol):
    async def list_candidates(
        self, scope: EvidenceScope, *, max_chunks: int = 500
    ) -> tuple[EvidenceHit, ...]:
        """先选最新可见版本再过滤条件；超过上限须失败，不能偏置截断。"""
        ...

    async def get_vectors(
        self, profile: EmbeddingProfile, chunk_ids: tuple[UUID, ...]
    ) -> tuple[VectorEntry, ...]: ...

    async def save_vectors(self, entries: tuple[VectorEntry, ...]) -> None:
        """原子校验全文哈希/fixture 范围；相同索引幂等，不同内容不得覆盖。"""
        ...
