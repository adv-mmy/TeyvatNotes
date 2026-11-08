"""50题虚构评测的可复跑性、逐维失败、fixture漂移和零网络边界。"""

import asyncio
import json
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from teyvatnotes.application.guide_agent import AgentBudget, AgentResult
from teyvatnotes.application.guide_answering import AnswerClaim, AnswerResult
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.knowledge import EvidenceScope
from teyvatnotes.knowledge import agent_cli, evaluation, evaluation_cli
from teyvatnotes.knowledge.evaluation import (
    fixture_repository,
    grade_result,
    percentile,
    run_evaluation,
)
from teyvatnotes.knowledge.evaluation_cases import (
    CHARACTER,
    EvaluationCase,
    Reference,
    fixed_cases,
)
from teyvatnotes.knowledge.reliability import probe_service


def test_fixed_corpus_counts_splits_and_manual_annotations() -> None:
    cases = fixed_cases()
    assert len(cases) == 50 and len({case.id for case in cases}) == 50
    assert Counter(case.category for case in cases) == {
        "character": 15,
        "quest": 15,
        "missing_conditions": 10,
        "conflict": 5,
        "out_of_scope": 5,
    }
    development = {case.id for case in cases if case.split == "development"}
    holdout = {case.id for case in cases if case.split == "holdout"}
    assert len(development) == 30 and len(holdout) == 20 and not development & holdout
    assert all(case.request.game_version == "DEMO-ONLY" for case in cases[:30])
    assert all(case.required_step_ordinals == (0, 1, 2, 3) for case in cases[15:30])
    assert all(
        case.expected_status == "insufficient_evidence" and len(case.references) == 2
        for case in cases
        if case.category == "conflict"
    )


@pytest.mark.parametrize("case", fixed_cases(), ids=lambda case: case.id)
def test_every_fixed_case_reports_real_result_instead_of_changing_expectation(
    case: EvaluationCase,
) -> None:
    report = asyncio.run(run_evaluation((case,)))
    record = report["cases"][0]
    assert record["process_passed"] is (case.category != "conflict")
    assert record["manual_semantic_review_required"] is True
    assert record["checks"]["semantic_citation_support"] is None
    assert record["checks"]["game_fact_correctness"] is None
    assert record["answer"]["model_called"] is False and record["answer"]["cost"] is None
    if case.category in {"missing_conditions", "out_of_scope"}:
        assert record["answer"]["generation_attempts"] == record["answer"]["tool_attempts"] == 0
    if case.category == "conflict":
        assert record["failure_reasons"] == ["expected_status", "reference_read"]
        assert record["read_reference_recall"] == 0.5
    assert record["events"][0]["kind"] == "answer_started"
    assert record["events"][-1]["kind"] == "answer_finished"


def test_manifest_and_failure_classification_are_repeatable() -> None:
    first, second = asyncio.run(run_evaluation()), asyncio.run(run_evaluation())
    assert first["case_manifest_sha256"] == second["case_manifest_sha256"]
    assert first["fixture_sha256"] == second["fixture_sha256"]
    assert first["summary"]["passed"] == 45 and first["summary"]["failed"] == 5
    assert first["summary"]["groups"]["split"]["holdout"] == {"total": 20, "passed": 18}
    assert first["summary"]["cost"] is None and first["summary"]["input_tokens"] is None
    assert first["summary"]["publication_ready"] is False
    assert first["summary"]["real_api_calls"] == 0
    assert first["summary"]["simulated_model_attempts"] == 105
    assert first["summary"]["semantic_quality_verified"] is False
    assert [record["failure_reasons"] for record in first["cases"]] == [
        record["failure_reasons"] for record in second["cases"]
    ]


def test_cli_demo_ignores_deployment_and_paid_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key in ("DATABASE_URL", "TEST_DATABASE_URL", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setenv(key, "PRIVATE")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("不允许读取部署配置或付费调用")

    monkeypatch.setattr(agent_cli, "create_database", forbidden)
    monkeypatch.setattr(agent_cli, "configured_tool_client", forbidden)
    assert evaluation_cli.main(["demo"]) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["status"] == "completed_with_findings" and len(report["cases"]) == 50
    assert "PRIVATE" not in output and "encrypted_content" not in output


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "PRIVATE"},
        {"category": []},
        {"split": True},
        {"expected_status": {}},
        {"references": []},
        {"references": ()},
        {"required_step_ordinals": (True,)},
        {"request": None},
    ],
)
def test_invalid_case_is_rejected_before_execution(changes: dict[str, object]) -> None:
    with pytest.raises(GuideImportError, match="invalid_evaluation_case"):
        replace(fixed_cases()[0], **changes)


@pytest.mark.parametrize(
    "source,ordinal", [("", 0), (True, 0), ("source", True), ("source", -1), ("source", 21)]
)
def test_reference_types_are_strict(source, ordinal) -> None:
    with pytest.raises(GuideImportError, match="invalid_evaluation_reference"):
        Reference(source, ordinal)


@pytest.mark.parametrize("cases", [(), [], (fixed_cases()[0],) * 2, fixed_cases() + fixed_cases()])
def test_dataset_bounds_and_duplicate_ids_reject(cases) -> None:
    with pytest.raises(GuideImportError, match="invalid_evaluation_cases"):
        asyncio.run(run_evaluation(cases))


def test_fixture_hash_drift_does_not_silently_relabel_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evaluation, "FIXTURE_HASHES", {"retrieval_character.json": "0" * 64})
    with pytest.raises(GuideImportError, match="evaluation_fixture_drift"):
        asyncio.run(run_evaluation())


def test_missing_reference_fails_before_any_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> agent_cli.OfflineToolClient:
        raise AssertionError("不应生成")

    case = replace(fixed_cases()[0], references=(Reference("not-present", 0),))
    monkeypatch.setattr(evaluation, "OfflineToolClient", forbidden)
    with pytest.raises(GuideImportError, match="evaluation_reference_missing"):
        asyncio.run(run_evaluation((case,)))


def test_grader_separates_unowned_resources_wrong_ids_and_step_order() -> None:
    async def scenario() -> None:
        _, catalog = await fixture_repository()
        service = await probe_service(agent_cli.OfflineToolClient(), AgentBudget())
        original = await service.answer(CHARACTER)
        case = fixed_cases()[0]
        altered_claim = replace(
            original.answer.claims[0],
            required_equipment=("未拥有装备",),
            evidence_ids=(str(uuid4()),),
        )
        bad = replace(original, answer=replace(original.answer, claims=(altered_claim,)))
        checks = grade_result(case, bad, (), catalog)["checks"]
        assert checks["explicit_resources_owned"] is False and checks["citation_id_valid"] is False
        assert checks["semantic_citation_support"] is None
        quest = fixed_cases()[15]
        claim_ids = [catalog[Reference("1000000001", ordinal)] for ordinal in range(4)]
        # 借真实fixture窗口建立合法ID，倒序引用相邻step仍必须报告顺序错误。
        repo, _ = await fixture_repository()

        scope = EvidenceScope(
            "DEMO-ONLY", quest.request.guide_kind, "示例任务甲", quest.request.conditions, True
        )

        window = await repo.read_evidence(UUID(claim_ids[2]), scope)
        claims = tuple(AnswerClaim("虚构", "fact", "step", (claim_ids[i],)) for i in (0, 2, 1, 3))
        result = AgentResult(
            AnswerResult("answer", claims=claims, evidence=(window,), demo_only=True)
        )
        checks = grade_result(quest, result, (), catalog)["checks"]
        assert checks["step_evidence_complete"] is True and checks["step_evidence_order"] is False
        assert checks["quest_sections_complete_and_ordered"] is False
        foreign_chunk = replace(original.answer.evidence[0].chunks[0], document_id=uuid4())
        foreign_window = replace(original.answer.evidence[0], chunks=(foreign_chunk,))
        polluted = replace(original, answer=replace(original.answer, evidence=(foreign_window,)))
        assert (
            grade_result(case, polluted, (), catalog)["checks"]["version_entity_conditions_valid"]
            is False
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "problem,failed_check",
    [
        ("missing_citation", "citation_id_valid"),
        ("character_not_owned", "explicit_resources_owned"),
        ("wrong_version", "version_entity_conditions_valid"),
        ("wrong_entity", "version_entity_conditions_valid"),
        ("wrong_conditions", "version_entity_conditions_valid"),
        ("truncated", "context_not_truncated"),
        ("empty_answer", "answer_structure"),
        ("unexpected_status", "expected_status"),
    ],
)
def test_grader_reports_each_independent_negative_dimension(
    problem: str, failed_check: str
) -> None:
    async def scenario() -> None:
        _, catalog = await fixture_repository()
        original = await (await probe_service(agent_cli.OfflineToolClient(), AgentBudget())).answer(
            CHARACTER
        )
        answer = original.answer
        window = answer.evidence[0]
        searches = ((window.hit,),)
        if problem == "missing_citation":
            answer = replace(answer, claims=(replace(answer.claims[0], evidence_ids=()),))
        elif problem == "character_not_owned":
            answer = replace(
                answer, claims=(replace(answer.claims[0], required_characters=("不存在角色",)),)
            )
        elif problem.startswith("wrong_"):
            document = window.hit.document
            if problem == "wrong_version":
                document = replace(document, game_version="other-version")
            else:
                data = deepcopy(document.snapshot)
                data["entities" if problem == "wrong_entity" else "conditions"] = (
                    ["其他角色"] if problem == "wrong_entity" else {"装备": "不存在装备"}
                )
                document = replace(document, snapshot=data)
            hit = replace(window.hit, document=document)
            answer = replace(answer, evidence=(replace(window, hit=hit),))
        elif problem == "truncated":
            answer = replace(answer, evidence=(replace(window, context_truncated=True),))
        elif problem == "empty_answer":
            answer = replace(answer, claims=())
        else:
            answer = replace(answer, status="error", claims=(), reason="simulated_error")
        grade = grade_result(fixed_cases()[0], replace(original, answer=answer), searches, catalog)
        assert grade["checks"][failed_check] is False and grade["process_passed"] is False
        assert failed_check in grade["failure_reasons"]
        assert grade["checks"]["game_fact_correctness"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "values,fraction,expected", [((), 0.5, None), ((9, 1, 4), 0.5, 4), ((9, 1, 4), 0.95, 9)]
)
def test_percentiles_are_nearest_rank(values, fraction, expected) -> None:
    assert percentile(values, fraction) == expected


@pytest.mark.parametrize("fraction", [True, 0, -1, 1.1, float("nan"), float("inf")])
def test_percentile_parameters_reject_non_finite_or_bool(fraction) -> None:
    with pytest.raises(GuideImportError, match="invalid_evaluation_metric"):
        percentile((1,), fraction)


def test_cli_error_output_is_stable_and_does_not_leak_private_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def failing():
        raise RuntimeError("PRIVATE")

    monkeypatch.setattr(evaluation_cli, "run_evaluation", failing)
    assert evaluation_cli.main(["demo"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "evaluation_unavailable",
    }
