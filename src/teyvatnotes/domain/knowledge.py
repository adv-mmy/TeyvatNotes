"""关键词检索的可验证范围、结果与纯规则，不依赖数据库或模型。"""

from dataclasses import dataclass

from teyvatnotes.domain.guides import (
    DocumentStatus,
    GuideChunk,
    GuideDocument,
    GuideImportError,
    GuideKind,
)


def bounded_text(value: object, maximum: int) -> bool:
    """运行时检查字符串；类型标注本身不会校验来自边界的数据。"""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def conditions_are_valid(value: object) -> bool:
    """检查不可变键值元组、文本预算与键唯一性，复用于请求与检索边界。"""
    if not isinstance(value, tuple) or len(value) > 20:
        return False
    if any(
        not isinstance(pair, tuple)
        or len(pair) != 2
        or not bounded_text(pair[0], 80)
        or not bounded_text(pair[1], 160)
        for pair in value
    ):
        return False
    return len(dict(value)) == len(value)


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    """精确版本/实体与玩家已确认条件；演示范围只能读虚构草稿。"""

    game_version: str
    guide_kind: GuideKind
    entity: str
    conditions: tuple[tuple[str, str], ...] = ()
    demo_only: bool = False

    def __post_init__(self) -> None:
        if (
            not bounded_text(self.game_version, 80)
            or self.game_version.strip().lower() in {"unknown", "未知"}
            or not isinstance(self.guide_kind, GuideKind)
            or not bounded_text(self.entity, 80)
            or not isinstance(self.demo_only, bool)
            or not conditions_are_valid(self.conditions)
        ):
            raise GuideImportError("invalid_knowledge_query")


@dataclass(frozen=True, slots=True)
class KeywordQuery:
    """显式字面关键词；不是自然语言理解、分词、向量相似度或置信度。"""

    scope: EvidenceScope
    keywords: tuple[str, ...]
    limit: int = 5

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, EvidenceScope)
            or not isinstance(self.keywords, tuple)
            or not 1 <= len(self.keywords) <= 12
            or any(not bounded_text(term, 80) for term in self.keywords)
            or not isinstance(self.limit, int)
            or isinstance(self.limit, bool)
            or not 1 <= self.limit <= 10
        ):
            raise GuideImportError("invalid_knowledge_query")
        # 统一大小写并去重，重复关键词不能人为提高分数。
        object.__setattr__(self, "keywords", tuple(dict.fromkeys(t.lower() for t in self.keywords)))


@dataclass(frozen=True, slots=True)
class EvidenceHit:
    """一个库内文本块与其不可变文档版本；score 只表示字面命中。"""

    document: GuideDocument
    chunk: GuideChunk
    score: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    """命中块及有序上下文；截断时不能宣称已补齐任务前置步骤。"""

    hit: EvidenceHit
    chunks: tuple[GuideChunk, ...]
    context_truncated: bool


def version_in_scope(document: GuideDocument, scope: EvidenceScope) -> bool:
    """先选可见版本，再做实体/条件/关键词过滤，防止回退到过时资料。"""
    expected_status = DocumentStatus.DRAFT if scope.demo_only else DocumentStatus.PUBLISHED
    return (
        document.status is expected_status
        and document.game_version == scope.game_version
        and document.guide_kind is scope.guide_kind
        and document.snapshot.get("is_fixture") is scope.demo_only
        and document.snapshot.get("complete") is True
    )


def document_in_scope(document: GuideDocument, scope: EvidenceScope) -> bool:
    """资料要求的每个条件都必须被玩家确认；未提供不等于已满足。"""
    entities = document.snapshot.get("entities")
    conditions = document.snapshot.get("conditions")
    return (
        version_in_scope(document, scope)
        and isinstance(entities, list)
        and scope.entity in entities
        and isinstance(conditions, dict)
        and all(dict(scope.conditions).get(key) == value for key, value in conditions.items())
    )


def keyword_score(chunk: GuideChunk, keywords: tuple[str, ...]) -> int:
    """每词正文命中加 4、章节命中加 2；不根据出现次数放大分数。"""
    text, section = chunk.text.lower(), chunk.section.lower()
    return sum(4 * (term in text) + 2 * (term in section) for term in keywords)


def context_range(hit: EvidenceHit) -> tuple[int, int]:
    """任务补取从开头到命中后一块；角色补取左右相邻块，边界含末项。"""
    start = 0 if hit.document.guide_kind is GuideKind.QUEST else max(0, hit.chunk.ordinal - 1)
    return start, hit.chunk.ordinal + 1


def require_context_limit(max_chunks: int) -> None:
    """限制单次原文回读，bool 不能作为整数预算。"""
    if not isinstance(max_chunks, int) or isinstance(max_chunks, bool) or not 1 <= max_chunks <= 20:
        raise GuideImportError("invalid_knowledge_query")
