"""RAG CLI 对比、引用回读、范围过滤与正式配置门禁。"""

import json
from uuid import uuid4

import pytest

from teyvatnotes.knowledge.rag_cli import main
from teyvatnotes.knowledge.rag_evaluation import retrieval_metrics

SEARCH = [
    "search",
    "--text",
    "第二步示例机关",
    "--keyword",
    "机关",
    "--version",
    "DEMO-ONLY",
    "--kind",
    "quest",
    "--entity",
    "示例任务甲",
    "--condition",
    "前置任务=已完成示例前置",
]


@pytest.mark.parametrize("mode", ["keyword", "vector", "hybrid"])
def test_demo_search_returns_scores_and_preceding_steps(
    mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*SEARCH, "--demo", "--mode", mode]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == mode and result["candidate_count"] == 4
    assert not result["answer_model_called"] and not result["paid_embedding_called"]
    assert result["semantic_embedding"] is False and result["cost"] is None
    hit = next(item for item in result["hits"] if item["evidence"]["ordinal"] == 2)
    assert [chunk["ordinal"] for chunk in hit["context"]["context"]] == [0, 1, 2, 3]
    assert not hit["context"]["context_truncated"]
    assert hit["evidence"]["citation_url"].startswith("https://www.miyoushe.com/")
    if mode != "keyword":
        assert "lexical_vectors_not_semantic" in result["warnings"]


def test_context_truncation_remains_explicit(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([*SEARCH, "--demo", "--mode", "keyword", "--max-chunks", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["hits"][0]["context"]["context_truncated"]


def test_comparison_reports_negative_cases_and_does_not_claim_real_quality(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["compare", "--demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["case_count"] == 12 and len(result["runs"]) == 36
    assert "not_answer_quality_evaluation" in result["warnings"]
    for summary in result["summary"]:
        assert summary["positive_cases"] == 8
        assert (
            summary["negative_hits"] == summary["scope_violations"] == summary["context_gaps"] == 0
        )
        assert 0 <= summary["macro_recall_at_k"] <= 1 and 0 <= summary["mrr"] <= 1
    miss = next(
        run for run in result["runs"] if run["case"] == "词面无命中" and run["mode"] == "keyword"
    )
    assert miss["recall_at_k"] == 0


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_formal_vector_modes_do_not_probe_sol_or_read_secrets(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "SECRET-not-a-database-url")
    monkeypatch.setenv("LLM_API_KEY", "PRIVATE")
    assert main([*SEARCH, "--mode", mode]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "embedding_not_configured"}


def test_formal_keyword_requires_database_and_compare_requires_demo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main([*SEARCH, "--mode", "keyword"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "database_not_configured"}
    assert main(["compare"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "retrieval_comparison_demo_required"}


def test_comparison_ranking_metrics_deduplicate_recall() -> None:
    first, second, other = uuid4(), uuid4(), uuid4()
    assert retrieval_metrics((other, first, first), frozenset((first, second))) == (0.5, 0.5)
    assert retrieval_metrics((first,), frozenset()) == (0, 0)


@pytest.mark.parametrize("limit", ["0", "11"])
def test_compare_invalid_budget_fails_before_loading_demo(
    limit: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def forbidden() -> None:
        raise AssertionError("不应加载样本")

    monkeypatch.setattr("teyvatnotes.knowledge.rag_cli.create_indexed_demo", forbidden)
    assert main(["compare", "--demo", "--limit", limit]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "invalid_rag_budget"}
