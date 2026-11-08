"""不调用模型的问答前置用例：追问必要字段、检索及补取库内证据。"""

from dataclasses import dataclass, replace
from typing import Literal

from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.domain.guides import GuideImportError, GuideKind
from teyvatnotes.domain.knowledge import (
    EvidenceScope,
    EvidenceWindow,
    KeywordQuery,
    bounded_text,
    conditions_are_valid,
)


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    """显式携带玩家字段与关键词，不从自由文本臆测版本、实体或进度。"""

    question: str
    guide_kind: GuideKind | None = None
    game_version: str | None = None
    entity: str | None = None
    keywords: tuple[str, ...] = ()
    conditions: tuple[tuple[str, str], ...] = ()
    goal: str | None = None
    location: str | None = None
    progress: str | None = None
    owned_equipment: tuple[str, ...] | None = None
    owned_characters: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """直接应用调用也守住边界；Schema 不是运行时唯一校验层。"""
        if not bounded_text(self.question, 1000) or (
            self.guide_kind is not None and not isinstance(self.guide_kind, GuideKind)
        ):
            raise GuideImportError("invalid_question")
        for value, maximum in (
            (self.game_version, 80),
            (self.entity, 80),
            (self.goal, 80),
            (self.location, 80),
            (self.progress, 160),
        ):
            if value is not None and not bounded_text(value, maximum):
                raise GuideImportError("invalid_question")
        if (
            not isinstance(self.keywords, tuple)
            or len(self.keywords) > 12
            or any(not bounded_text(term, 80) for term in self.keywords)
        ):
            raise GuideImportError("invalid_question")
        if not conditions_are_valid(self.conditions):
            raise GuideImportError("invalid_question")
        for inventory in (self.owned_equipment, self.owned_characters):
            if inventory is not None and (
                not isinstance(inventory, tuple)
                or len(inventory) > 100
                or any(not bounded_text(item, 80) for item in inventory)
                or len(set(inventory)) != len(inventory)
            ):
                raise GuideImportError("invalid_question")


@dataclass(frozen=True, slots=True)
class QuestionPreparation:
    """evidence_ready 仅表示取得范围内证据，不表示答案或事实审核通过。"""

    status: Literal["needs_clarification", "insufficient_evidence", "evidence_ready"]
    questions: tuple[str, ...] = ()
    evidence: tuple[EvidenceWindow, ...] = ()
    reason: str | None = None
    demo_only: bool = False


class QuestionPreparationService:
    """先确定必要条件，再执行有界只读操作；本次没有模型费用或网络副作用。"""

    def __init__(self, reader: KnowledgeReader, *, demo_only: bool = False) -> None:
        if not isinstance(demo_only, bool):
            raise GuideImportError("invalid_knowledge_query")
        self._reader = reader
        self._demo_only = demo_only

    async def prepare(self, request: QuestionRequest) -> QuestionPreparation:
        """缺字段不访问仓储；证据不足/上下文截断时不宣称具备完整攻略依据。"""
        if not bounded_text(request.question, 1000):
            raise GuideImportError("invalid_question")
        questions: list[str] = []
        if request.guide_kind is None:
            questions.append("你要查询角色培养还是任务解谜？")
        if not request.game_version or request.game_version.strip().lower() in {"unknown", "未知"}:
            questions.append("请提供本次查询的准确游戏版本。")
        if not request.entity:
            questions.append("请提供要查询的角色或任务名称（与资料实体一致）。")
        if not request.keywords:
            questions.append("请提供检索关键词；当前尚不自动理解自由文本。")
        if request.guide_kind is GuideKind.CHARACTER and not request.goal:
            questions.append("这次角色培养的目标是装备、配队还是其他方向？")
        if request.guide_kind is GuideKind.QUEST:
            if not request.location:
                questions.append("你当前在哪个地点？")
            if not request.progress:
                questions.append("你已经完成了哪些步骤或前置任务？")
        if questions:
            return QuestionPreparation(
                "needs_clarification", tuple(questions), demo_only=self._demo_only
            )
        # 上方检查后显式缩窄 Optional，不用 assert 代替公开输入校验。
        if request.game_version is None or request.entity is None or request.guide_kind is None:
            raise GuideImportError("invalid_question")
        scope = EvidenceScope(
            request.game_version,
            request.guide_kind,
            request.entity,
            request.conditions,
            self._demo_only,
        )
        hits = await self._reader.search_knowledge(KeywordQuery(scope, request.keywords))
        if not hits:
            return QuestionPreparation(
                "insufficient_evidence", reason="no_matching_evidence", demo_only=self._demo_only
            )
        windows_list: list[EvidenceWindow] = []
        for hit in hits:
            window = await self._reader.read_evidence(hit.chunk.id, scope)
            # 回读没有排名分；问答准备保留原检索分，不将 0 误当模型置信度。
            windows_list.append(replace(window, hit=replace(window.hit, score=hit.score)))
        windows = tuple(windows_list)
        if any(window.context_truncated for window in windows):
            return QuestionPreparation(
                "insufficient_evidence",
                evidence=windows,
                reason="context_truncated",
                demo_only=self._demo_only,
            )
        return QuestionPreparation("evidence_ready", evidence=windows, demo_only=self._demo_only)
