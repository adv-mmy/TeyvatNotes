"""有界、可复跑的虚构流程评测；指标不冒充语义支持或真实游戏质量。"""

import asyncio
import math
from collections import Counter
from functools import partial
from time import perf_counter
from uuid import UUID

from teyvatnotes.application.answer_events import AnswerEventRecorder
from teyvatnotes.application.conversations import question_to_data
from teyvatnotes.application.guide_agent import (
    AGENT_PROMPT,
    AgentResult,
    GuideAgentService,
)
from teyvatnotes.application.guide_ingestion import (
    PROCESSING_VERSION,
    GuideIngestionService,
    fingerprint,
)
from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.question_preparation import QuestionPreparationService
from teyvatnotes.domain import JsonValue
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.knowledge import (
    EvidenceHit,
    EvidenceScope,
    EvidenceWindow,
    KeywordQuery,
    document_in_scope,
)
from teyvatnotes.knowledge.agent_cli import (
    OfflineToolClient,
    agent_to_json,
    tool_session,
)
from teyvatnotes.knowledge.answers import JsonAnswerDecoder
from teyvatnotes.knowledge.demo import PROJECT_ROOT
from teyvatnotes.knowledge.evaluation_cases import (
    CORPUS_REVISION,
    FIXTURE_HASHES,
    EvaluationCase,
    Reference,
    fixed_cases,
)
from teyvatnotes.knowledge.snapshots import load_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository


class RecordingKnowledgeReader:
    """只记录模型工具的实际搜索；问答预检不算工具检索命中。"""

    def __init__(self, reader: KnowledgeReader) -> None:
        self._reader = reader
        self.searches: list[tuple[EvidenceHit, ...]] = []

    async def search_knowledge(self, query: KeywordQuery) -> tuple[EvidenceHit, ...]:
        hits = await self._reader.search_knowledge(query)
        if len(self.searches) >= 3 or len(hits) > 10:
            raise GuideImportError("evaluation_observation_budget_exceeded")
        self.searches.append(hits)
        return hits

    async def read_evidence(
        self,
        chunk_id: UUID,
        scope: EvidenceScope,
        *,
        max_chunks: int = 20,
    ) -> EvidenceWindow:
        return await self._reader.read_evidence(chunk_id, scope, max_chunks=max_chunks)


async def fixture_repository() -> tuple[InMemoryKnowledgeRepository, dict[Reference, str]]:
    """先校验全部快照哈希；漂移失败，不根据当前检索结果反推参考。"""
    snapshots = []
    for name, expected_hash in FIXTURE_HASHES.items():
        snapshot = await asyncio.to_thread(load_snapshot, PROJECT_ROOT / "samples/guides" / name)
        if fingerprint(snapshot.data) != expected_hash:
            raise GuideImportError("evaluation_fixture_drift")
        snapshots.append(snapshot)
    repo = InMemoryKnowledgeRepository()
    catalog: dict[Reference, str] = {}
    for snapshot in snapshots:
        report = await GuideIngestionService(repo).import_snapshot(snapshot)
        for chunk in await repo.list_chunks(report.document_id):
            catalog[Reference(snapshot.source.native_id, chunk.ordinal)] = str(chunk.id)
    return repo, catalog


def percentile(values: tuple[int, ...], fraction: float) -> int | None:
    """nearest-rank百分位；无数据未知，不设Runner性能硬阈值。"""
    if (
        not isinstance(values, tuple)
        or any(type(value) is not int or value < 0 for value in values)
        or isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not 0 < fraction <= 1
    ):
        raise GuideImportError("invalid_evaluation_metric")
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def grade_result(
    case: EvaluationCase,
    result: AgentResult,
    searches: tuple[tuple[EvidenceHit, ...], ...],
    catalog: dict[Reference, str],
) -> dict[str, JsonValue]:
    """参考、引用存在性、范围、显式资源和标注步骤逐维检查，语义项保持未评估。"""
    if any(reference not in catalog for reference in case.references):
        raise GuideImportError("evaluation_reference_missing")
    answer, request = result.answer, case.request
    expected = {catalog[ref] for ref in case.references}
    retrieved = {str(hit.chunk.id) for hits in searches for hit in hits}
    first = [str(hit.chunk.id) for hit in searches[0]] if searches else []
    reciprocal = next(
        (1 / index for index, identifier in enumerate(first, 1) if identifier in expected), 0.0
    )
    ledger = {str(chunk.id): chunk for window in answer.evidence for chunk in window.chunks}
    cited = {identifier for claim in answer.claims for identifier in claim.evidence_ids}
    citation_valid = all(
        claim.evidence_ids and set(claim.evidence_ids) <= ledger.keys() for claim in answer.claims
    )
    scoped_hits = tuple(hit for hits in searches for hit in hits) + tuple(
        window.hit for window in answer.evidence
    )
    scope_valid = not scoped_hits
    if scoped_hits and request.game_version and request.entity and request.guide_kind:
        try:
            scope = EvidenceScope(
                request.game_version,
                request.guide_kind,
                request.entity,
                request.conditions,
                answer.demo_only,
            )
            scope_valid = all(
                document_in_scope(hit.document, scope) for hit in scoped_hits
            ) and all(
                chunk.document_id == window.hit.document.id
                for window in answer.evidence
                for chunk in window.chunks
            )
        except GuideImportError:
            scope_valid = False
    resources_valid = all(
        set(claim.required_equipment) <= set(request.owned_equipment or ())
        and set(claim.required_characters) <= set(request.owned_characters or ())
        for claim in answer.claims
    )
    step_ids = (
        {
            catalog[Reference(case.references[0].source_id, ordinal)]
            for ordinal in case.required_step_ordinals
        }
        if case.required_step_ordinals and case.references
        else set()
    )
    step_sequence = [
        ledger[identifier].ordinal
        for claim in answer.claims
        for identifier in claim.evidence_ids
        if identifier in step_ids and identifier in ledger
    ]
    quest_sections_valid = True
    if (
        request.guide_kind is not None
        and request.guide_kind.value == "quest"
        and answer.status == "answer"
    ):
        sections = {claim.section for claim in answer.claims}
        order = {"prerequisite": 0, "step": 1, "completion": 2}
        positions = [order[claim.section] for claim in answer.claims if claim.section in order]
        quest_sections_valid = {
            "prerequisite",
            "step",
            "completion",
        } <= sections and positions == sorted(positions)
    structure_valid = (
        (
            answer.status == "answer"
            and bool(answer.claims)
            and not answer.questions
            and not answer.conflicts
        )
        or (answer.status == "needs_clarification" and bool(answer.questions) and not answer.claims)
        or (
            answer.status == "insufficient_evidence"
            and bool(answer.gaps or answer.conflicts or answer.reason)
            and not answer.claims
        )
        or (answer.status == "error" and bool(answer.reason) and not answer.claims)
    )
    checks: dict[str, JsonValue] = {
        "expected_status": answer.status == case.expected_status,
        "answer_structure": structure_valid,
        "reference_retrieved": expected <= retrieved,
        "reference_read": expected <= ledger.keys(),
        "citation_id_valid": citation_valid,
        "version_entity_conditions_valid": scope_valid,
        "explicit_resources_owned": resources_valid,
        "context_not_truncated": not any(window.context_truncated for window in answer.evidence),
        "step_evidence_complete": step_ids <= cited,
        "step_evidence_order": step_sequence == sorted(step_sequence),
        "quest_sections_complete_and_ordered": quest_sections_valid,
        "call_budget": len(result.model_turns) <= 4 and len(result.tool_calls) <= 3,
        "missing_or_outside_zero_calls": not result.model_turns and not result.tool_calls
        if case.category in {"missing_conditions", "out_of_scope"}
        else True,
        "semantic_citation_support": None,
        "game_fact_correctness": None,
    }
    failures: list[JsonValue] = [name for name, value in checks.items() if value is False]
    return {
        "process_passed": not failures,
        "checks": checks,
        "failure_reasons": failures,
        "tool_search_reference_recall": len(expected & retrieved) / len(expected)
        if expected
        else None,
        "first_tool_search_mrr": reciprocal if expected else None,
        "read_reference_recall": len(expected & ledger.keys()) / len(expected)
        if expected
        else None,
        "manual_semantic_review_required": True,
    }


def summarize(records: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """分组成功数及耗时，未知费用保持null；不把模拟调用次数当真实API次数。"""
    passed = sum(record["process_passed"] is True for record in records)
    groups: dict[str, JsonValue] = {}
    for field in ("category", "split"):
        grouped: dict[str, JsonValue] = {}
        for label in sorted({str(record[field]) for record in records}):
            items = [record for record in records if record[field] == label]
            grouped[label] = {
                "total": len(items),
                "passed": sum(item["process_passed"] is True for item in items),
            }
        groups[field] = grouped
    durations = tuple(
        value
        for record in records
        if isinstance(value := record["elapsed_ms"], int) and not isinstance(value, bool)
    )
    failures = Counter(
        str(reason)
        for record in records
        for reason in (
            record["failure_reasons"] if isinstance(record["failure_reasons"], list) else []
        )
    )
    simulated_attempts = 0
    for record in records:
        answer = record.get("answer")
        if isinstance(answer, dict):
            attempts = answer.get("generation_attempts")
            if type(attempts) is int:
                simulated_attempts += attempts
    return {
        "total": len(records),
        "passed": passed,
        "failed": len(records) - passed,
        "groups": groups,
        "failures": dict(failures),
        "latency_ms_p50": percentile(durations, 0.5),
        "latency_ms_p95": percentile(durations, 0.95),
        "real_api_calls": 0,
        "simulated_model_attempts": simulated_attempts,
        "input_tokens": None,
        "output_tokens": None,
        "cost": None,
        "publication_ready": False,
        "semantic_quality_verified": False,
    }


async def run_evaluation(
    cases: tuple[EvaluationCase, ...] | None = None,
) -> dict[str, JsonValue]:
    """顺序最多50题，默认纯离线；预检和每轮工具授权均保持现有边界。"""
    selected = fixed_cases() if cases is None else cases
    if (
        not isinstance(selected, tuple)
        or not 1 <= len(selected) <= 50
        or any(not isinstance(case, EvaluationCase) for case in selected)
        or len({case.id for case in selected}) != len(selected)
    ):
        raise GuideImportError("invalid_evaluation_cases")
    for case in selected:
        # 防止直接绕过dataclass构造；不允许运行后才发现坏参考。
        EvaluationCase(
            case.id,
            case.category,
            case.split,
            case.request,
            case.expected_status,
            case.references,
            case.required_step_ordinals,
        )
    repo, catalog = await fixture_repository()
    for case in selected:
        references = (
            (
                *case.references,
                *(
                    Reference(case.references[0].source_id, ordinal)
                    for ordinal in case.required_step_ordinals
                ),
            )
            if case.references
            else ()
        )
        if any(ref not in catalog for ref in references):
            raise GuideImportError("evaluation_reference_missing")
    records: list[dict[str, JsonValue]] = []
    for case in selected:
        observed = RecordingKnowledgeReader(repo)
        service = GuideAgentService(
            QuestionPreparationService(repo, demo_only=True),
            OfflineToolClient,
            partial(tool_session, observed, demo_only=True),
            JsonAnswerDecoder(),
            demo_only=True,
        )
        started = perf_counter()
        events = AnswerEventRecorder()
        result = await service.answer(case.request, events=events)
        grade = grade_result(case, result, tuple(observed.searches), catalog)
        records.append(
            {
                "case_id": case.id,
                "category": case.category,
                "split": case.split,
                "input": question_to_data(case.request),
                "expected_status": case.expected_status,
                "reference_ids": [catalog[ref] for ref in case.references],
                "tool_searches": [
                    [str(hit.chunk.id) for hit in hits] for hits in observed.searches
                ],
                "answer": agent_to_json(result, case.request),
                "events": [
                    {
                        "sequence": event.sequence,
                        "kind": event.kind,
                        "index": event.index,
                        "status": event.status,
                        "error": event.error,
                    }
                    for event in events.events
                ],
                "elapsed_ms": round((perf_counter() - started) * 1000),
                **grade,
            }
        )
    case_data: list[JsonValue] = [
        {
            "id": case.id,
            "input": question_to_data(case.request),
            "expected_status": case.expected_status,
            "references": [
                {"source_id": ref.source_id, "ordinal": ref.ordinal} for ref in case.references
            ],
            "category": case.category,
            "split": case.split,
            "required_step_ordinals": list(case.required_step_ordinals),
        }
        for case in selected
    ]
    return {
        "status": "completed_with_findings"
        if any(record["process_passed"] is False for record in records)
        else "ok",
        "evaluation_kind": "fictional_process_regression",
        "demo_only": True,
        "model_called": False,
        "corpus_revision": CORPUS_REVISION,
        "case_manifest_sha256": fingerprint(case_data),
        "fixture_sha256": dict(FIXTURE_HASHES),
        "prompt_sha256": fingerprint(AGENT_PROMPT),
        "schema_sha256": fingerprint(JsonAnswerDecoder().schema()),
        "processing_version": f"{PROCESSING_VERSION}:chars-600",
        "simulator": "offline-search-read-extract-v1",
        "summary": summarize(records),
        "cases": list(records),
        "warnings": [
            "fictional_demo_only",
            "simulated_model_no_network",
            "cost_unknown",
            "not_fact_verified",
            "semantic_support_not_evaluated",
            "holdout_shares_fixtures_not_blind_quality_test",
        ],
    }
