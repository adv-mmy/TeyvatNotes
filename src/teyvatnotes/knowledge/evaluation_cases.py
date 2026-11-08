"""50个固定虚构过程用例；参考来自人工标注，不读取运行结果来生成期望。"""

import re
from dataclasses import dataclass, replace
from typing import Literal

from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.domain.guides import GuideImportError, GuideKind

Category = Literal["character", "quest", "missing_conditions", "conflict", "out_of_scope"]
Split = Literal["development", "holdout"]
CORPUS_REVISION = "fictional-guide-v1"
FIXTURE_HASHES = {
    "retrieval_character.json": "2b50b3bfe26d12880a1484842324fd897966d13d79794448e682ee8102e4dba2",
    "retrieval_quest.json": "fee614032d6fea71aa4d32f6ccb0d4aac4a71db9da105e60b3ab818e8d3b8cbf",
    "evaluation_conflict_a.json": (
        "ec576d8bcf21bfab6e39a922687a62376a2da0e8ba698f3e43860dbdc36e31db"
    ),
    "evaluation_conflict_b.json": (
        "003754ac6237c9f73b8f8f3df8775f48c9c14909f11893cce0872bfb5c352930"
    ),
}


@dataclass(frozen=True, slots=True)
class Reference:
    source_id: str
    ordinal: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_id, str)
            or not self.source_id.strip()
            or (type(self.ordinal) is not int or not 0 <= self.ordinal <= 20)
        ):
            raise GuideImportError("invalid_evaluation_reference")


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    category: Category
    split: Split
    request: QuestionRequest
    expected_status: Literal["answer", "needs_clarification", "insufficient_evidence"]
    references: tuple[Reference, ...] = ()
    required_step_ordinals: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or re.fullmatch(r"[a-z_]+_\d{2}", self.id) is None
            or not isinstance(self.category, str)
            or self.category
            not in {"character", "quest", "missing_conditions", "conflict", "out_of_scope"}
            or not isinstance(self.split, str)
            or self.split not in {"development", "holdout"}
            or not isinstance(self.expected_status, str)
            or self.expected_status
            not in {"answer", "needs_clarification", "insufficient_evidence"}
            or not isinstance(self.request, QuestionRequest)
            or not isinstance(self.references, tuple)
            or len(self.references) > 8
            or any(not isinstance(ref, Reference) for ref in self.references)
            or len(set(self.references)) != len(self.references)
            or not isinstance(self.required_step_ordinals, tuple)
            or any(
                type(item) is not int or not 0 <= item <= 20 for item in self.required_step_ordinals
            )
            or len(set(self.required_step_ordinals)) != len(self.required_step_ordinals)
            or (self.expected_status == "answer" and not self.references)
            or (
                self.required_step_ordinals
                and (not self.references or self.request.guide_kind is not GuideKind.QUEST)
            )
        ):
            raise GuideImportError("invalid_evaluation_case")
        replace(self.request)
        for reference in self.references:
            Reference(reference.source_id, reference.ordinal)


CHARACTER = QuestionRequest(
    "示例角色甲的装备原文是什么？（完全虚构）",
    GuideKind.CHARACTER,
    "DEMO-ONLY",
    "示例角色甲",
    ("装备",),
    (("装备", "示例木剑"),),
    "装备",
    owned_equipment=("示例木剑",),
    owned_characters=("示例角色甲",),
)
QUEST = QuestionRequest(
    "示例任务甲第二步怎样按原文操作？（完全虚构）",
    GuideKind.QUEST,
    "DEMO-ONLY",
    "示例任务甲",
    ("第二步",),
    (("前置任务", "已完成示例前置"),),
    location="虚构地点",
    progress="第一步已完成",
)


def fixed_cases() -> tuple[EvaluationCase, ...]:
    """同一9块虚构文本的输入变体；不是50个独立真实知识点或盲测集。"""
    cases: list[EvaluationCase] = []
    character_queries = (
        ("装备", "装备资料如何核查", 1),
        ("操作", "操作资料在什么位置", 2),
        ("适用条件", "示例资料适用什么条件", 0),
    )
    quest_queries = (
        ("第二步", "请核查第二步及前后步骤", 2),
        ("机关", "示例机关的顺序原文是什么", 2),
        ("完成标志", "怎样核查任务完成提示", 3),
    )
    groups: tuple[tuple[Category, QuestionRequest, tuple[tuple[str, str, int], ...], str], ...] = (
        ("character", CHARACTER, character_queries, "BV1DEMO00002"),
        ("quest", QUEST, quest_queries, "1000000001"),
    )
    for category, base, queries, source in groups:
        for index in range(15):
            keyword, question, ordinal = queries[index % 3]
            cases.append(
                EvaluationCase(
                    f"{category}_{index + 1:02d}",
                    category,
                    "development" if index < 9 else "holdout",
                    replace(
                        base, question=f"{question}？虚构输入变体{index + 1}。", keywords=(keyword,)
                    ),
                    "answer",
                    (Reference(source, ordinal),),
                    (0, 1, 2, 3) if category == "quest" else (),
                )
            )
    missing = (
        replace(CHARACTER, owned_equipment=None),
        replace(CHARACTER, game_version=None),
        replace(CHARACTER, entity=None),
        replace(CHARACTER, guide_kind=None),
        replace(CHARACTER, goal=None),
        replace(CHARACTER, keywords=()),
        replace(CHARACTER, goal="配队", owned_characters=None),
        replace(QUEST, location=None),
        replace(QUEST, progress=None),
        replace(QUEST, game_version="unknown"),
    )
    for index, request in enumerate(missing):
        cases.append(
            EvaluationCase(
                f"missing_{index + 1:02d}",
                "missing_conditions",
                "development" if index < 6 else "holdout",
                request,
                "needs_clarification",
            )
        )
    for index, question in enumerate(
        (
            "相同条件下示例开关应开启还是关闭？",
            "两篇虚构资料结论相反，应该怎样处理？",
            "是否可以只相信冲突资料甲？",
            "冲突资料乙是否与甲一致？",
            "请说明开关资料仍有哪些缺口。",
        )
    ):
        cases.append(
            EvaluationCase(
                f"conflict_{index + 1:02d}",
                "conflict",
                "development" if index < 3 else "holdout",
                replace(
                    CHARACTER, question=question, entity="示例冲突角色", keywords=("冲突机制",)
                ),
                "insufficient_evidence",
                (Reference("BV1DEMO00004", 0), Reference("BV1DEMO00005", 0)),
            )
        )
    outside = (
        replace(CHARACTER, game_version="DEMO-OTHER"),
        replace(CHARACTER, entity="不存在角色"),
        replace(QUEST, entity="不存在任务"),
        replace(QUEST, keywords=("不存在关键词",)),
        replace(CHARACTER, conditions=(("装备", "不存在装备"),)),
    )
    for index, request in enumerate(outside):
        cases.append(
            EvaluationCase(
                f"outside_{index + 1:02d}",
                "out_of_scope",
                "development" if index < 3 else "holdout",
                request,
                "insufficient_evidence",
            )
        )
    return tuple(cases)
