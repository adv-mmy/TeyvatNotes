"""开发者专名校对：测试词表中除用户确认名外的名称均为虚构用例。"""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from test_guide_ingestion import article_snapshot

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.domain.guides import GuideImportError, GuideKind, StepName
from teyvatnotes.domain.knowledge import EvidenceScope, KeywordQuery
from teyvatnotes.domain.terminology import (
    TermCategory,
    TermDefinition,
    TerminologyCatalog,
)
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.knowledge.terminology import (
    MAX_CATALOG_BYTES,
    load_catalog,
    parse_catalog,
)
from teyvatnotes.knowledge.terminology_cli import main
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository


def catalog() -> TerminologyCatalog:
    return TerminologyCatalog(
        "test-v1",
        (
            TermDefinition(TermCategory.CHARACTER, "奥黛塔", ("奥萝塔",), "用户确认"),
            TermDefinition(TermCategory.WEAPON, "风语剑", ("风语劍",), "虚构测试"),
            TermDefinition(TermCategory.ARTIFACT, "翠影之章", ("翠影套",), "虚构测试"),
        ),
    )


@pytest.mark.parametrize(
    "category,original,target",
    [
        (TermCategory.CHARACTER, "奥萝塔", "奥黛塔"),
        (TermCategory.WEAPON, "风语劍", "风语剑"),
        (TermCategory.ARTIFACT, "翠影套", "翠影之章"),
    ],
)
def test_confirmed_aliases_resolve_only_in_their_category(category, original, target) -> None:
    terms = catalog()
    result = terms.resolve(original, category)
    assert result.name == target and result.status == "corrected"
    assert terms.resolve(target, category).status == "canonical"
    other = TermCategory.WEAPON if category is not TermCategory.WEAPON else TermCategory.CHARACTER
    assert terms.resolve(original, other).status == "needs_review"
    assert terms.resolve(original, other).name == original


def test_unknown_similar_name_is_a_candidate_not_an_automatic_replacement() -> None:
    result = catalog().resolve("奥戴塔", TermCategory.CHARACTER)
    assert result.name == "奥戴塔" and result.status == "needs_review"
    assert result.candidates == ("奥黛塔",)
    assert catalog().correct_text("奥戴塔上场").text == "奥戴塔上场"


def test_cross_category_alias_ambiguity_preserves_text_and_requires_review() -> None:
    terms = TerminologyCatalog(
        "v1",
        (
            TermDefinition(TermCategory.WEAPON, "雪之剑", ("同名错误",), "测试"),
            TermDefinition(TermCategory.ARTIFACT, "雪之章", ("同名错误",), "测试"),
        ),
    )
    assert terms.resolve("同名错误", TermCategory.WEAPON).name == "雪之剑"
    result = terms.correct_text("装备同名错误。")
    assert result.text == "装备同名错误。" and result.ambiguous_names == ("同名错误",)


def test_text_changes_are_single_pass_and_keep_both_offset_coordinates() -> None:
    result = catalog().correct_text("翠影套，奥萝塔E技能。")
    assert result.text == "翠影之章，奥黛塔E技能。"
    assert len(result.edits) == 2
    assert (result.edits[0].source_start, result.edits[0].source_end) == (0, 3)
    assert (result.edits[0].normalized_start, result.edits[0].normalized_end) == (0, 4)
    assert result.edits[1].normalized_start == result.edits[1].source_start + 1
    assert catalog().correct_text(result.text).edits == ()


def test_long_canonical_name_protects_shorter_alias_and_ascii_word_boundaries() -> None:
    terms = TerminologyCatalog(
        "v1",
        (
            TermDefinition(TermCategory.WEAPON, "风之剑", ("风语剑", "Mist"), "测试"),
            TermDefinition(TermCategory.ARTIFACT, "长风语剑", (), "测试"),
        ),
    )
    assert terms.correct_text("长风语剑，风语剑").text == "长风语剑，风之剑"
    assert terms.correct_text("Mist Mystery Misty").text == "风之剑 Mystery Misty"


@pytest.mark.parametrize("text", ["奥萝塔尔", "超级奥萝塔", "奥萝塔配队", "奥萝塔𠀀"])
def test_unseparated_alias_cannot_rewrite_part_of_an_unknown_longer_name(text: str) -> None:
    result = catalog().correct_text(text)
    assert result.text == text and result.edits == ()
    assert result.ambiguous_names == ("奥萝塔",)


def test_large_catalog_limits_fuzzy_work_without_losing_exact_corrections() -> None:
    terms = TerminologyCatalog(
        "large-v1",
        (
            *catalog().terms,
            *(
                TermDefinition(TermCategory.CHARACTER, f"虚构角色{i}", (), "测试")
                for i in range(257)
            ),
        ),
    )
    unknown = terms.resolve("奥戴塔", TermCategory.CHARACTER)
    assert unknown.name == "奥戴塔" and unknown.status == "needs_review"
    assert unknown.suggestions_limited is True and unknown.candidates == ()
    assert terms.resolve("奥萝塔", TermCategory.CHARACTER).name == "奥黛塔"
    assert terms.resolve("风语劍", TermCategory.WEAPON).suggestions_limited is False


def test_one_character_canonical_and_empty_catalog_are_supported() -> None:
    terms = TerminologyCatalog("v1", (TermDefinition(TermCategory.CHARACTER, "魈", (), "测试"),))
    assert terms.resolve("魈", TermCategory.CHARACTER).status == "canonical"
    empty = TerminologyCatalog("empty-v1", ())
    assert empty.correct_text("未登记原文").text == "未登记原文"
    assert empty.resolve("未登记原文").candidates == ()


@pytest.mark.parametrize(
    "change",
    [
        {"revision": ""},
        {"extra": "PRIVATE"},
        {"terms": [{"category": "spell", "name": "角色", "confirmation": "测试"}]},
        {
            "terms": [
                {"category": "weapon", "name": "角色", "aliases": ["角"], "confirmation": "测试"}
            ]
        },
        {
            "terms": [
                {"category": "weapon", "name": "角色", "aliases": ["角色"], "confirmation": "测试"}
            ]
        },
        {"terms": [{"category": "weapon", "name": " 角色", "confirmation": "测试"}]},
    ],
)
def test_catalog_rejects_ambiguous_or_invalid_developer_data(change) -> None:
    payload = catalog().to_json()
    payload.update(change)
    with pytest.raises(GuideImportError):
        parse_catalog(payload)


def test_duplicate_aliases_within_category_are_rejected() -> None:
    with pytest.raises(GuideImportError, match="ambiguous_terminology_catalog"):
        TerminologyCatalog(
            "v1",
            (
                TermDefinition(TermCategory.WEAPON, "雪之剑", ("同名错误",), "测试"),
                TermDefinition(TermCategory.WEAPON, "风之剑", ("同名错误",), "测试"),
            ),
        )


def test_catalog_hash_ignores_entry_order_but_tracks_content_and_revision() -> None:
    original = catalog()
    assert (
        replace(original, terms=tuple(reversed(original.terms))).content_hash
        == original.content_hash
    )
    assert replace(original, revision="test-v2").content_hash != original.content_hash
    assert replace(original, terms=original.terms[:-1]).content_hash != original.content_hash


def test_default_catalog_uses_reviewed_wiki_names_and_keeps_confirmed_aliases() -> None:
    terms = load_catalog()
    assert terms.revision == "2026-10-04.2"
    assert {
        category: sum(term.category is category for term in terms.terms)
        for category in TermCategory
    } == {TermCategory.CHARACTER: 129, TermCategory.WEAPON: 252, TermCategory.ARTIFACT: 63}
    assert all(
        "2026-10-04" in term.confirmation
        and "https://baike.mihoyo.com/ys/obc/content/" in term.confirmation
        and "非逐页详情审核" in term.confirmation
        for term in terms.terms
    )
    assert terms.resolve("奥萝塔", TermCategory.CHARACTER).name == "奥黛塔"
    assert {term.name: term.aliases for term in terms.terms if term.aliases} == {
        "奥黛塔": ("奥萝塔",)
    }
    for category, name in (
        (TermCategory.CHARACTER, "魈"),
        (TermCategory.WEAPON, "蝶变"),
        (TermCategory.ARTIFACT, "血红之证"),
    ):
        assert terms.resolve(name, category).status == "canonical"


def test_wiki_catalog_keeps_category_fuzzy_budgets_and_does_not_guess_nicknames() -> None:
    terms = load_catalog()
    for category in TermCategory:
        assert terms.resolve("未知名称", category).suggestions_limited is False
    assert terms.resolve("未知名称").suggestions_limited is True
    result = terms.resolve("冰主", TermCategory.CHARACTER)
    assert result.name == "冰主" and result.status == "needs_review"
    assert terms.resolve("旅行者·冰", TermCategory.CHARACTER).status == "canonical"


@pytest.mark.parametrize("mode", ["empty", "omitted", "null"])
def test_developer_aliases_can_be_empty_or_omitted_but_not_null(mode: str) -> None:
    payload = {
        "revision": "test-v1",
        "terms": [{"category": "character", "name": "标准角色", "confirmation": "虚构测试"}],
    }
    if mode != "omitted":
        payload["terms"][0]["aliases"] = [] if mode == "empty" else None
    if mode == "null":
        with pytest.raises(GuideImportError, match="invalid_terminology_catalog"):
            parse_catalog(payload)
    else:
        assert parse_catalog(payload).terms[0].aliases == ()


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"{bad",
        b' {"revision":"v1","revision":"v2","terms":[]}',
        b" " * (MAX_CATALOG_BYTES + 1),
    ],
    ids=["invalid-utf8", "invalid-json", "duplicate-key", "over-size-limit"],
)
def test_file_loader_rejects_bad_utf8_duplicate_keys_and_size(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "terms.json"
    path.write_bytes(raw)
    with pytest.raises(GuideImportError):
        load_catalog(path)


def test_import_corrects_entities_and_evidence_without_overwriting_source() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        original = article_snapshot(
            guide_kind="character",
            entities=["奥萝塔"],
            recognized_terms=[{"category": "character", "name": "奥萝塔"}],
            article_blocks=[
                {"kind": "heading", "text": "奥萝塔：配队"},
                {"kind": "paragraph", "text": "翠影套，奥萝塔。"},
            ],
        )
        snapshot = parse_snapshot(original)
        before = deepcopy(snapshot.data)
        service = GuideIngestionService(repository, terminology_catalog=catalog())
        imported = await service.import_snapshot(snapshot, publish=True)
        assert snapshot.data == before
        document = await repository.get_document(imported.document_id)
        assert document.snapshot["entities"] == ["奥黛塔"]
        assert document.snapshot["article_blocks"][1]["text"] == "翠影套，奥萝塔。"
        chunk = (await repository.list_chunks(imported.document_id))[0]
        assert chunk.text == "翠影之章，奥黛塔。" and chunk.section == "奥黛塔：配队"
        audit = chunk.locators[0]["terminology"]
        assert audit["source_text"] == "翠影套，奥萝塔。"
        assert (
            audit["offset_basis"] == "normalized_text" and audit["source_section"] == "奥萝塔：配队"
        )
        scope = EvidenceScope("test-version", GuideKind.CHARACTER, "奥黛塔")
        assert (await repository.search_knowledge(KeywordQuery(scope, ("奥黛塔",))))[
            0
        ].chunk == chunk
        assert (await service.import_snapshot(snapshot)).reused_steps == (
            StepName.PARSE,
            StepName.CHUNK,
            StepName.VALIDATE,
        )
        assert document.snapshot["_terminology"]["catalog_hash"] == catalog().content_hash

    asyncio.run(scenario())


def test_changed_catalog_creates_new_document_and_keeps_old_evidence() -> None:
    async def scenario() -> None:
        repo = InMemoryKnowledgeRepository()
        source = parse_snapshot(
            article_snapshot(article_blocks=[{"kind": "paragraph", "text": "奥萝塔：配队"}])
        )
        old_catalog = TerminologyCatalog("old-v1", ())
        first = await GuideIngestionService(repo, terminology_catalog=old_catalog).import_snapshot(
            source, publish=True
        )
        second = await GuideIngestionService(repo, terminology_catalog=catalog()).import_snapshot(
            source, publish=True
        )
        assert first.document_id != second.document_id
        assert "奥萝塔" in (await repo.list_chunks(first.document_id))[0].text
        assert "奥黛塔" in (await repo.list_chunks(second.document_id))[0].text
        assert (await repo.get_document(first.document_id)).snapshot["_terminology"][
            "catalog_hash"
        ] == old_catalog.content_hash

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["奥戴塔", "未登记名称"])
def test_unconfirmed_declared_name_blocks_publication_without_auto_guessing(name: str) -> None:
    async def scenario() -> None:
        repo = InMemoryKnowledgeRepository()
        source = parse_snapshot(
            article_snapshot(recognized_terms=[{"category": "character", "name": name}])
        )
        service = GuideIngestionService(repo, terminology_catalog=catalog())
        report = await service.import_snapshot(source)
        assert "terminology_review_required" in report.blocking_issues
        with pytest.raises(GuideImportError, match="publication_blocked"):
            await service.import_snapshot(source, publish=True)

    asyncio.run(scenario())


def test_ambiguous_body_alias_blocks_even_without_declared_terms() -> None:
    async def scenario() -> None:
        terms = TerminologyCatalog(
            "v1",
            (
                TermDefinition(TermCategory.WEAPON, "雪之剑", ("同名错误",), "测试"),
                TermDefinition(TermCategory.ARTIFACT, "雪之章", ("同名错误",), "测试"),
            ),
        )
        repo = InMemoryKnowledgeRepository()
        source = parse_snapshot(
            article_snapshot(article_blocks=[{"kind": "paragraph", "text": "同名错误"}])
        )
        report = await GuideIngestionService(repo, terminology_catalog=terms).import_snapshot(
            source
        )
        assert report.blocking_issues == ("terminology_review_required",)
        assert (await repo.list_chunks(report.document_id))[0].text == "同名错误"

    asyncio.run(scenario())


def test_unseparated_body_alias_requires_review_before_publication() -> None:
    async def scenario() -> None:
        repo = InMemoryKnowledgeRepository()
        source = parse_snapshot(
            article_snapshot(article_blocks=[{"kind": "paragraph", "text": "奥萝塔配队"}])
        )
        service = GuideIngestionService(repo, terminology_catalog=catalog())
        report = await service.import_snapshot(source)
        assert report.blocking_issues == ("terminology_review_required",)
        assert (await repo.list_chunks(report.document_id))[0].text == "奥萝塔配队"
        with pytest.raises(GuideImportError, match="publication_blocked"):
            await service.import_snapshot(source, publish=True)

    asyncio.run(scenario())


def test_cli_name_check_is_offline_and_does_not_claim_unknown_names_are_correct(capsys) -> None:
    assert main(["check-name", "奥萝塔", "--category", "character"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["name"] == "奥黛塔" and result["model_called"] is False
    assert main(["check-name", "奥戴塔", "--category", "character"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["name"] == "奥戴塔" and result["status"] == "needs_review"


def test_invalid_catalog_error_does_not_echo_private_file_or_content(tmp_path, capsys) -> None:
    path = tmp_path / "private.json"
    path.write_text('{"private":"SECRET"}', encoding="utf-8")
    assert main(["check-name", "奥萝塔", "--category", "character", "--catalog", str(path)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "invalid_terminology_catalog",
    }
