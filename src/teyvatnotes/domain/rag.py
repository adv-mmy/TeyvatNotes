"""可追溯的向量契约与检索结果；分数不是答案置信度。"""

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from teyvatnotes.domain.guides import GuideChunk, GuideImportError
from teyvatnotes.domain.knowledge import EvidenceHit, EvidenceScope, bounded_text


def require_budget(value: int, maximum: int = 500) -> None:
    """bool 虽是 int 子类，但不能作为数量预算。"""
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise GuideImportError("invalid_rag_budget")


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """索引身份包含模型、算法修订及维度；词面演示不能用于正式资料。"""

    provider: str
    model: str
    revision: str
    dimensions: int
    demo_only: bool = False
    semantic: bool = True

    def __post_init__(self) -> None:
        if (
            any(not bounded_text(value, 80) for value in (self.provider, self.model, self.revision))
            or not isinstance(self.demo_only, bool)
            or not isinstance(self.semantic, bool)
            or (not self.semantic and not self.demo_only)
        ):
            raise GuideImportError("invalid_embedding_profile")
        require_budget(self.dimensions, 4096)

    @property
    def identity(self) -> str:
        payload = json.dumps(
            [
                self.provider,
                self.model,
                self.revision,
                self.dimensions,
                self.demo_only,
                self.semantic,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def embedding_text(chunk: GuideChunk) -> str:
    """保留章节和原文，不用摘要替代证据；修改此规则须更新 profile.revision。"""
    return f"{chunk.section}\n{chunk.text}"


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unit_vector(values: tuple[float, ...], dimensions: int) -> tuple[float, ...]:
    """先缩放再归一化，避免有限大数求平方溢出；拒绝零向量和 NaN。"""
    if (
        not isinstance(values, tuple)
        or len(values) != dimensions
        or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values)
    ):
        raise GuideImportError("invalid_embedding_vector")
    try:
        finite_values = tuple(float(value) for value in values)
    except (OverflowError, ValueError) as exc:
        raise GuideImportError("invalid_embedding_vector") from exc
    if any(not math.isfinite(value) for value in finite_values):
        raise GuideImportError("invalid_embedding_vector")
    scale = max((abs(value) for value in finite_values), default=0)
    if scale == 0:
        raise GuideImportError("invalid_embedding_vector")
    scaled = tuple(value / scale for value in finite_values)
    norm = math.sqrt(math.fsum(value * value for value in scaled))
    return tuple(value / norm for value in scaled)


def cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """精确余弦距离计算；这里只处理有界小规模候选，不是 ANN 索引。"""
    a, b = unit_vector(left, len(left)), unit_vector(right, len(left))
    return max(-1.0, min(1.0, math.fsum(x * y for x, y in zip(a, b, strict=True))))


@dataclass(frozen=True, slots=True)
class VectorEntry:
    chunk_id: UUID
    profile: EmbeddingProfile
    content_hash: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.chunk_id, UUID)
            or not isinstance(self.profile, EmbeddingProfile)
            or not isinstance(self.content_hash, str)
            or len(self.content_hash) != 64
            or any(c not in "0123456789abcdef" for c in self.content_hash)
        ):
            raise GuideImportError("invalid_vector_entry")
        # 保存原始浮点值，归一化只在评分时进行；重建索引仍能核对原始输出。
        unit_vector(self.values, self.profile.dimensions)


class RetrievalMode(StrEnum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class RagQuery:
    scope: EvidenceScope
    text: str
    keywords: tuple[str, ...] = ()
    mode: RetrievalMode = RetrievalMode.HYBRID
    limit: int = 5

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, EvidenceScope)
            or not bounded_text(self.text, 1000)
            or not isinstance(self.mode, RetrievalMode)
            or not isinstance(self.keywords, tuple)
            or len(self.keywords) > 12
            or any(not bounded_text(term, 80) for term in self.keywords)
            or (self.mode is RetrievalMode.KEYWORD and not self.keywords)
        ):
            raise GuideImportError("invalid_rag_query")
        require_budget(self.limit, 10)
        object.__setattr__(self, "keywords", tuple(dict.fromkeys(t.lower() for t in self.keywords)))


@dataclass(frozen=True, slots=True)
class RankedEvidence:
    evidence: EvidenceHit
    keyword_score: int
    vector_score: float | None
    keyword_rank: int | None
    vector_rank: int | None
    fused_score: float


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    hits: tuple[RankedEvidence, ...]
    mode: RetrievalMode
    candidate_count: int
    indexed_count: int
    profile: EmbeddingProfile | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IndexResult:
    document_id: UUID
    profile: EmbeddingProfile
    chunk_count: int
    generated_count: int
    reused_count: int
