"""固定虚构问题的检索对比；不评价真实游戏事实、语义或模型答案。"""

import time
from dataclasses import dataclass
from uuid import UUID

from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.rag_ports import RagRepository
from teyvatnotes.application.rag_service import RagSearchService, read_context
from teyvatnotes.domain import JsonValue
from teyvatnotes.domain.guides import GuideImportError, GuideKind
from teyvatnotes.domain.knowledge import (
    EvidenceScope,
    context_range,
    document_in_scope,
)
from teyvatnotes.domain.rag import RagQuery, RetrievalMode


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    """参考证据是人工标注的 fixture ordinal，不是运行结果反推的答案。"""

    name: str
    kind: GuideKind
    entity: str
    text: str
    keywords: tuple[str, ...]
    relevant_ordinals: tuple[int, ...]
    version: str = "DEMO-ONLY"
    conditions: tuple[tuple[str, str], ...] = ()

    @property
    def scope(self) -> EvidenceScope:
        return EvidenceScope(self.version, self.kind, self.entity, self.conditions, True)


CHARACTER_CONDITIONS = (("装备", "示例木剑"),)
QUEST_CONDITIONS = (("前置任务", "已完成示例前置"),)
FIXED_CASES = (
    RetrievalCase(
        "角色装备",
        GuideKind.CHARACTER,
        "示例角色甲",
        "示例木剑装备说明在哪里？",
        ("装备",),
        (1,),
        conditions=CHARACTER_CONDITIONS,
    ),
    RetrievalCase(
        "角色操作",
        GuideKind.CHARACTER,
        "示例角色甲",
        "操作教学如何引用？",
        ("操作",),
        (2,),
        conditions=CHARACTER_CONDITIONS,
    ),
    RetrievalCase(
        "角色条件",
        GuideKind.CHARACTER,
        "示例角色甲",
        "本样本完全虚构的适用条件是什么？",
        ("适用条件",),
        (0,),
        conditions=CHARACTER_CONDITIONS,
    ),
    RetrievalCase(
        "任务前置",
        GuideKind.QUEST,
        "示例任务甲",
        "开始之前需要已完成哪些前置？",
        ("开始之前",),
        (0,),
        conditions=QUEST_CONDITIONS,
    ),
    RetrievalCase(
        "任务第一步",
        GuideKind.QUEST,
        "示例任务甲",
        "第一步检查示例按钮",
        ("按钮",),
        (1,),
        conditions=QUEST_CONDITIONS,
    ),
    RetrievalCase(
        "任务第二步",
        GuideKind.QUEST,
        "示例任务甲",
        "第二步示例机关为什么要在第一步后检查？",
        ("机关",),
        (2,),
        conditions=QUEST_CONDITIONS,
    ),
    RetrievalCase(
        "任务完成",
        GuideKind.QUEST,
        "示例任务甲",
        "看到示例提示的完成标志是什么？",
        ("完成标志",),
        (3,),
        conditions=QUEST_CONDITIONS,
    ),
    RetrievalCase("角色缺条件", GuideKind.CHARACTER, "示例角色甲", "装备教学", ("装备",), ()),
    RetrievalCase(
        "前置不满足",
        GuideKind.QUEST,
        "示例任务甲",
        "示例机关",
        ("机关",),
        (),
        conditions=(("前置任务", "尚未完成"),),
    ),
    RetrievalCase(
        "版本不匹配",
        GuideKind.QUEST,
        "示例任务甲",
        "示例机关",
        ("机关",),
        (),
        version="OTHER-DEMO",
        conditions=QUEST_CONDITIONS,
    ),
    RetrievalCase(
        "未知实体",
        GuideKind.CHARACTER,
        "不存在的示例角色",
        "装备",
        ("装备",),
        (),
        conditions=CHARACTER_CONDITIONS,
    ),
    RetrievalCase(
        "词面无命中",
        GuideKind.QUEST,
        "示例任务甲",
        "第一步检查示例按钮",
        ("不存在的字面词",),
        (1,),
        conditions=QUEST_CONDITIONS,
    ),
)


def retrieval_metrics(returned: tuple[UUID, ...], relevant: frozenset[UUID]) -> tuple[float, float]:
    """Recall@k 与首个相关命中的倒数排名；负例单独统计，不混入宏平均。"""
    if not relevant:
        return 0.0, 0.0
    recall = len(set(returned) & relevant) / len(relevant)
    reciprocal = next(
        (1 / rank for rank, chunk_id in enumerate(returned, 1) if chunk_id in relevant), 0.0
    )
    return recall, reciprocal


async def compare_retrieval(
    service: RagSearchService, repository: RagRepository, reader: KnowledgeReader, *, limit: int = 3
) -> dict[str, JsonValue]:
    """在同一 fixture 候选库比较三种排序，并独立核查范围和回读连续性。"""
    runs: list[JsonValue] = []
    summaries: list[JsonValue] = []
    for mode in RetrievalMode:
        total_recall = total_reciprocal = 0.0
        positive_count = negative_hits = scope_violations = context_gaps = truncated = 0
        for case in FIXED_CASES:
            candidates = await repository.list_candidates(case.scope)
            relevant = frozenset(
                hit.chunk.id for hit in candidates if hit.chunk.ordinal in case.relevant_ordinals
            )
            # 标注找不到是样本漂移，不是检索算法失误，不能默默改成负例。
            if case.relevant_ordinals and len(relevant) != len(case.relevant_ordinals):
                raise GuideImportError("retrieval_fixture_drift")
            started = time.perf_counter()
            result = await service.search(
                RagQuery(case.scope, case.text, case.keywords, mode, limit)
            )
            returned = tuple(hit.evidence.chunk.id for hit in result.hits)
            recall, reciprocal = retrieval_metrics(returned, relevant)
            if relevant:
                positive_count += 1
                total_recall += recall
                total_reciprocal += reciprocal
            else:
                negative_hits += len(returned)
            details: list[JsonValue] = []
            for hit in result.hits:
                evidence = hit.evidence
                scope_violations += not document_in_scope(evidence.document, case.scope)
                window = await read_context(reader, hit, case.scope)
                truncated += window.context_truncated
                start, stop = context_range(evidence)
                available = {
                    item.chunk.ordinal
                    for item in candidates
                    if item.document.id == evidence.document.id
                }
                expected = {ordinal for ordinal in available if start <= ordinal <= stop}
                actual = {chunk.ordinal for chunk in window.chunks}
                gap = window.context_truncated or not expected <= actual
                context_gaps += gap
                details.append(
                    {
                        "evidence_id": str(evidence.chunk.id),
                        "document_id": str(evidence.document.id),
                        "ordinal": evidence.chunk.ordinal,
                        "game_version": evidence.document.game_version,
                        "keyword_score": hit.keyword_score,
                        "vector_score": hit.vector_score,
                        "keyword_rank": hit.keyword_rank,
                        "vector_rank": hit.vector_rank,
                        "fused_score": hit.fused_score,
                        "context_ordinals": [chunk.ordinal for chunk in window.chunks],
                        "context_truncated": window.context_truncated,
                        "context_gap": bool(gap),
                    }
                )
            runs.append(
                {
                    "case": case.name,
                    "mode": mode.value,
                    "expected_ids": [str(value) for value in sorted(relevant, key=str)],
                    "recall_at_k": recall if relevant else None,
                    "reciprocal_rank": reciprocal if relevant else None,
                    "hits": details,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
        summaries.append(
            {
                "mode": mode.value,
                "positive_cases": positive_count,
                "macro_recall_at_k": total_recall / positive_count if positive_count else None,
                "mrr": total_reciprocal / positive_count if positive_count else None,
                "negative_hits": negative_hits,
                "scope_violations": scope_violations,
                "context_gaps": context_gaps,
                "truncated_windows": truncated,
            }
        )
    return {
        "case_count": len(FIXED_CASES),
        "k": limit,
        "summary": summaries,
        "runs": runs,
        "warnings": [
            "fictional_demo_only",
            "lexical_vectors_not_semantic",
            "not_answer_quality_evaluation",
        ],
        "answer_model_called": False,
        "paid_embedding_called": False,
        "cost": None,
    }
