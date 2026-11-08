"""向量值对象校验、索引身份与确定性词面演示。"""

import asyncio
import math
from dataclasses import replace
from uuid import uuid4

import pytest

from teyvatnotes.domain.guides import GuideImportError, GuideKind
from teyvatnotes.domain.knowledge import EvidenceScope
from teyvatnotes.domain.rag import (
    EmbeddingProfile,
    RagQuery,
    RetrievalMode,
    VectorEntry,
    content_digest,
    cosine_similarity,
    unit_vector,
)
from teyvatnotes.knowledge.lexical_embedding import DemoLexicalEmbedding


@pytest.mark.parametrize("dimensions", [0, -1, 4097, True, 1.5])
def test_profile_rejects_invalid_dimensions(dimensions: int) -> None:
    with pytest.raises(GuideImportError):
        EmbeddingProfile("provider", "model", "revision", dimensions)


def test_profile_identity_separates_model_revision_dimension_and_scope() -> None:
    profile = EmbeddingProfile("p", "m", "r", 2)
    assert profile.identity == EmbeddingProfile("p", "m", "r", 2).identity
    alternatives = (
        replace(profile, model="n"),
        replace(profile, revision="r2"),
        replace(profile, dimensions=3),
        replace(profile, demo_only=True),
        replace(profile, demo_only=True, semantic=False),
    )
    assert len({profile.identity, *(p.identity for p in alternatives)}) == 6
    with pytest.raises(GuideImportError, match="invalid_embedding_profile"):
        replace(profile, semantic=False)


@pytest.mark.parametrize(
    "values",
    [(0.0, 0.0), (math.inf, 1.0), (math.nan, 1.0), (True, 1.0), (1.0,), (10**1000, 1.0)],
)
def test_vectors_reject_undefined_similarity(values: tuple[float, ...]) -> None:
    with pytest.raises(GuideImportError, match="invalid_embedding_vector"):
        VectorEntry(uuid4(), EmbeddingProfile("p", "m", "r", 2), content_digest("x"), values)


def test_similarity_handles_large_finite_values_and_negative_scores() -> None:
    vector = (1e308, 1e308)
    assert unit_vector(vector, 2) == pytest.approx((2**-0.5, 2**-0.5))
    assert cosine_similarity(vector, vector) == pytest.approx(1)
    assert cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == -1
    with pytest.raises(GuideImportError):
        cosine_similarity((1.0,), (1.0, 2.0))


def test_keyword_query_requires_terms_but_vector_can_use_natural_text() -> None:
    scope = EvidenceScope("DEMO-ONLY", GuideKind.CHARACTER, "虚构", demo_only=True)
    query = RagQuery(scope, "怎样培养？", ("装备", "装备"))
    assert query.keywords == ("装备",)
    assert RagQuery(scope, "怎样培养？", mode=RetrievalMode.VECTOR).keywords == ()
    with pytest.raises(GuideImportError):
        RagQuery(scope, "怎样培养？", mode=RetrievalMode.KEYWORD)


def test_demo_features_are_reproducible_not_semantic_or_gpt() -> None:
    async def scenario() -> None:
        client = DemoLexicalEmbedding()
        first = await client.embed(("装备 示例木剑", "前置 示范通行证"))
        assert first == await DemoLexicalEmbedding().embed(("装备 示例木剑", "前置 示范通行证"))
        assert len(first[0]) == client.profile.dimensions
        assert client.profile.demo_only and not client.profile.semantic
        assert client.profile.provider == "local_demo"
        assert cosine_similarity(first[0], first[0]) > cosine_similarity(first[0], first[1])

    asyncio.run(scenario())


@pytest.mark.parametrize("texts", [(), ("",), ("!",), ("x" * 8193,), ("x",) * 17])
def test_demo_input_budgets_are_enforced(texts: tuple[str, ...]) -> None:
    with pytest.raises(GuideImportError, match="invalid_embedding_input"):
        asyncio.run(DemoLexicalEmbedding().embed(texts))
