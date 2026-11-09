"""理解合同回归，不是实际模型准确率、检索或攻略答案质量评测。"""

import asyncio
import hashlib
import json
from collections import Counter
from time import perf_counter
from typing import Literal

from teyvatnotes.application.conversations import QUESTION_FIELDS, question_to_data
from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.model_ports import ModelRequest, ModelResponse, ModelUsage
from teyvatnotes.application.question_understanding import (
    EXTRACTION_PROMPT,
    QuestionUnderstandingService,
    UnderstandingResult,
)
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.json_types import JsonValue
from teyvatnotes.knowledge.terminology import load_catalog
from teyvatnotes.knowledge.understanding import JsonUnderstandingDecoder
from teyvatnotes.knowledge.understanding_cases import (
    CORPUS_HASH,
    CORPUS_ID,
    UnderstandingCase,
    fixed_understanding_cases,
)

Mode = Literal["rules", "simulated"]


def corpus_fingerprint(cases: tuple[UnderstandingCase, ...]) -> str:
    payload = {"id": CORPUS_ID, "cases": [c.to_json() for c in cases]}
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class FixedExtractionClient:
    """回放人工标注的候选或攻击计划，不根据原话自动推导，不接网络。"""

    def __init__(self, output: str) -> None:
        self._output, self.calls = output, 0

    def factory(self) -> "FixedExtractionClient":
        """将本案例的固定实例交给用例，避免延迟绑定循环变量。"""
        return self

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(self._output, "fixed-understanding-plan", ModelUsage(None, None))


def valid_sources(case: UnderstandingCase, result: UnderstandingResult) -> bool:
    if result.candidate_question is None:
        return result.proposed_updates is None and not result.field_sources
    values = question_to_data(result.candidate_question)
    context = question_to_data(case.context) if case.context is not None else {}
    fields: set[str] = set()
    for source in result.field_sources:
        if source.field not in QUESTION_FIELDS:
            return False
        fields.add(source.field)
        if source.source == "confirmed_context":
            if source.field not in context or context[source.field] != values[source.field]:
                return False
        elif source.source in {"explicit_text", "controlled_mapping"}:
            if not source.quote or source.quote not in case.text:
                return False
        else:
            return False
    required = set(result.proposed_updates.values) if result.proposed_updates is not None else set()
    for key, value in values.items():
        if value is not None and (
            value != [] and value != {} or key in {"owned_characters", "owned_equipment"}
        ):
            required.add(key)
    return required <= fields


def grade_understanding(
    case: UnderstandingCase,
    result: UnderstandingResult,
    mode: Mode,
    calls: int,
    remaining_calls: int,
) -> dict[str, JsonValue]:
    expected = case.model_expected if mode == "simulated" and case.model_expected else case.expected
    actual = result.to_json()
    expected_attempts = case.model_attempts if mode == "simulated" else 0
    return {
        "status": result.status == expected.status,
        "candidate": actual["candidate_question"] == expected.candidate,
        "patch": actual["proposed_updates"] == expected.patch,
        "missing_fields": tuple(sorted(result.missing_fields)) == tuple(sorted(expected.missing))
        and len(set(result.missing_fields)) == len(result.missing_fields),
        "reason": result.reason == expected.reason,
        "clarification": bool(result.questions)
        if expected.status == "needs_clarification"
        else not result.questions
        if expected.status == "ready"
        else True,
        "sources": valid_sources(case, result),
        "attempts": result.generation_attempts == calls == expected_attempts
        and remaining_calls == 4 - expected_attempts,
    }


async def run_understanding_evaluation(mode: str = "all") -> dict[str, JsonValue]:
    """只跑固定、公开、人工标注场景；哈希先校验，发现问题以失败行保留。"""
    if not isinstance(mode, str) or mode not in {"all", "rules", "simulated"}:
        raise GuideImportError("invalid_understanding_evaluation_mode")
    cases = fixed_understanding_cases()
    if not 1 <= len(cases) <= 100 or len({c.id for c in cases}) != len(cases):
        raise GuideImportError("invalid_understanding_evaluation_cases")
    fingerprint = corpus_fingerprint(cases)
    if fingerprint != CORPUS_HASH:
        raise GuideImportError("understanding_corpus_drift")
    catalog = load_catalog()
    modes: tuple[Mode, ...] = (
        ("rules", "simulated")
        if mode == "all"
        else ("rules",)
        if mode == "rules"
        else ("simulated",)
    )
    reports: list[JsonValue] = []
    total_failures, simulated_attempts = 0, 0
    started = perf_counter()
    for selected in modes:
        rows: list[JsonValue] = []
        passed, failures = 0, 0
        statuses: Counter[str] = Counter()
        passed_checks: Counter[str] = Counter()
        category_total: Counter[str] = Counter()
        category_passed: Counter[str] = Counter()
        accepted_total, accepted_matches = 0, 0
        for case in cases:
            ledger = GenerationLedger()
            client = FixedExtractionClient(case.model_output) if selected == "simulated" else None
            service = QuestionUnderstandingService(
                catalog,
                model_factory=client.factory if client else None,
                decoder=JsonUnderstandingDecoder() if client else None,
            )
            try:
                result = await service.understand(case.text, case.context, generation=ledger)
            except asyncio.CancelledError:
                raise
            except Exception:
                result = UnderstandingResult(
                    "error",
                    reason="understanding_evaluation_case_failed",
                    generation_attempts=client.calls if client else 0,
                )
            calls = client.calls if client else 0
            checks = grade_understanding(case, result, selected, calls, ledger.remaining_calls)
            success = all(checks.values())
            expected = (
                case.model_expected
                if selected == "simulated" and case.model_expected
                else case.expected
            )
            statuses[result.status] += 1
            category_total[case.category] += 1
            if success:
                passed += 1
                category_passed[case.category] += 1
            else:
                failures += 1
            for name, value in checks.items():
                passed_checks[name] += int(bool(value))
            if expected.candidate is not None:
                accepted_total += 1
                accepted_matches += int(bool(checks["candidate"]))
            observed = result.to_json()
            observed.pop("model_called")
            observed.pop("generation_attempts")
            observed["reported_extraction_attempts"] = result.generation_attempts
            observed["real_model_calls"] = 0
            observed["simulated_extraction_attempts"] = calls
            simulated_attempts += calls
            rows.append(
                {
                    "id": case.id,
                    "category": case.category,
                    "text": case.text,
                    "confirmed_context": question_to_data(case.context) if case.context else None,
                    "expected": expected.to_json(),
                    "observed": observed,
                    "passed": success,
                    "checks": checks,
                    "failed_checks": [k for k, v in checks.items() if not v],
                    "remaining_model_calls": ledger.remaining_calls,
                }
            )
        total_failures += failures
        reports.append(
            {
                "mode": selected,
                "executions": len(cases),
                "passed": passed,
                "failed": failures,
                "observed_statuses": dict(statuses),
                "metrics": {
                    "contract_pass_rate": passed / len(cases),
                    "check_pass_rates": {
                        k: v / len(cases) for k, v in sorted(passed_checks.items())
                    },
                    "accepted_candidate_exact_match_rate": accepted_matches / accepted_total
                    if accepted_total
                    else None,
                    "accepted_reference_count": accepted_total,
                },
                "categories": [
                    {"category": key, "total": count, "passed": category_passed[key]}
                    for key, count in sorted(category_total.items())
                ],
                "cases": rows,
            }
        )
    return {
        "status": "ok" if not total_failures else "completed_with_findings",
        "dataset_id": CORPUS_ID,
        "dataset_hash": fingerprint,
        "catalog_hash": catalog.content_hash,
        "prompt_hash": hashlib.sha256(EXTRACTION_PROMPT.encode("utf-8")).hexdigest(),
        "case_count": len(cases),
        "execution_count": len(cases) * len(modes),
        "distinct_input_context_count": len(
            {
                json.dumps(
                    {"text": c.text, "context": question_to_data(c.context) if c.context else None},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                for c in cases
            }
        ),
        "failed_executions": total_failures,
        "model_called": False,
        "real_model_calls": 0,
        "simulated_extraction_attempts": simulated_attempts,
        "input_tokens": None,
        "output_tokens": None,
        "cost": None,
        "elapsed_ms": round((perf_counter() - started) * 1000),
        "retrieval": {"status": "not_run", "errors": None},
        "answer": {"status": "not_run", "errors": None},
        "real_model_understanding_accuracy": None,
        "game_fact_correctness": None,
        "publication_ready": False,
        "reports": reports,
        "warnings": [
            "public_contract_regression_not_blind_test",
            "simulated_plan_not_real_model_accuracy",
            "not_retrieval_or_answer_quality",
            "private_report_do_not_publish",
        ],
    }
