"""Retrieval quality: hit rate at 3 for vector only and hybrid search over the 30 article knowledge base.

Everything runs offline. ``retrieval_fakes`` stands in for gte-small and for the
Postgres ``search_tsv`` column, and hybrid results go through the same
``reciprocal_rank_fusion`` arithmetic that ``match_kb_hybrid`` runs in SQL.
Run ``pytest -s tests/test_retrieval_quality.py`` to print the comparison table.
"""

import pytest
from retrieval_cases import RETRIEVAL_CASES
from retrieval_fakes import EMBEDDER_MODES, FakeEmbedder, OfflineKnowledgeBase

from triageops.kb_seed import KB_ARTICLES

SLUGS = {article.slug for article in KB_ARTICLES}
HYBRID_FLOOR = 0.85


def hit_rate(search, k: int = 3) -> float:
    hits = sum(expected in search(query, k) for query, expected in RETRIEVAL_CASES)
    return hits / len(RETRIEVAL_CASES)


@pytest.fixture(scope="module")
def results() -> dict[str, tuple[float, float]]:
    table = {}
    for mode in EMBEDDER_MODES:
        kb = OfflineKnowledgeBase(KB_ARTICLES, FakeEmbedder(mode))
        table[mode] = (hit_rate(kb.vector_only), hit_rate(kb.hybrid))
    print("\nhit rate at 3 over", len(RETRIEVAL_CASES), "queries")
    print(f"{'fake embedder':28} {'vector only':>12} {'hybrid':>8}")
    for mode, (vector, hybrid) in table.items():
        print(f"{mode:28} {vector:12.2f} {hybrid:8.2f}")
    return table


def test_there_are_twenty_cases_with_real_expected_articles() -> None:
    assert len(RETRIEVAL_CASES) == 20
    assert len({query for query, _ in RETRIEVAL_CASES}) == 20
    assert {expected for _, expected in RETRIEVAL_CASES} <= SLUGS


def test_cases_cover_most_of_the_new_articles() -> None:
    new_slugs = {article.slug for article in KB_ARTICLES[12:]}
    covered = {expected for _, expected in RETRIEVAL_CASES} & new_slugs
    assert len(covered) == 18


@pytest.mark.parametrize("mode", list(EMBEDDER_MODES))
def test_hybrid_is_never_worse_than_vector_only(results, mode: str) -> None:
    vector, hybrid = results[mode]
    assert hybrid >= vector, f"{mode}: hybrid {hybrid:.2f} fell below vector only {vector:.2f}"


@pytest.mark.parametrize("mode", list(EMBEDDER_MODES))
def test_hybrid_meets_the_quality_floor(results, mode: str) -> None:
    assert results[mode][1] >= HYBRID_FLOOR


def test_hybrid_beats_vector_only_on_average(results) -> None:
    vector = sum(v for v, _ in results.values()) / len(results)
    hybrid = sum(h for _, h in results.values()) / len(results)
    assert hybrid > vector


def test_fake_embedder_is_deterministic_and_normalised() -> None:
    first = FakeEmbedder("stems").embed("BitLocker recovery key")
    second = FakeEmbedder("stems").embed("BitLocker recovery key")
    assert first == second
    assert len(first) == 384
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9
