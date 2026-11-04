"""Hybrid retrieval: reciprocal rank fusion, the vector only fallback and the retry switch."""

from typing import Any

import psycopg
import pytest
from fakes import FakeStore
from psycopg import errors

from triageops import hybrid, rag, store, tools, triage
from triageops.hybrid import HybridSwitch, reciprocal_rank_fusion
from triageops.rag import KbMatch
from triageops.samples import SAMPLES_BY_KEY
from triageops.schemas import InboundTicket

ROW = {"slug": "bitlocker-recovery-key", "title": "BitLocker", "category": "Security", "content": "1. Verify the caller."}


@pytest.fixture(autouse=True)
def fresh_switch():
    hybrid.switch.reset()
    yield
    hybrid.switch.reset()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Fake embeddings plus recorders for both SQL functions."""
    recorded: list[tuple[str, Any]] = []
    monkeypatch.setattr(rag, "embed", lambda texts, timeout=0: [[0.1] * rag.EMBEDDING_DIMENSIONS for _ in texts])

    def vector(conn, literal, count):
        recorded.append(("vector", count))
        return [{**ROW, "similarity": 0.71}]

    def fused(conn, literal, text, count):
        recorded.append(("hybrid", text))
        return [{**ROW, "similarity": 0.64, "text_rank": 0.3, "rrf_score": 0.032}]

    monkeypatch.setattr(store, "match_kb", vector)
    monkeypatch.setattr(store, "match_kb_hybrid", fused)
    return recorded


# Reciprocal rank fusion -------------------------------------------------------------

def test_rrf_scores_are_summed_reciprocal_ranks() -> None:
    fused = dict(reciprocal_rank_fusion([["a", "b", "c"], ["c", "a"]], k=60))
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert fused["b"] == pytest.approx(1 / 62)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)


def test_rrf_rewards_agreement_between_retrievers() -> None:
    fused = reciprocal_rank_fusion([["solo", "both"], ["other", "both"]])
    assert fused[0][0] == "both"


def test_rrf_handles_empty_and_single_rankings() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert [key for key, _ in reciprocal_rank_fusion([["x", "y"], []])] == ["x", "y"]


def test_rrf_ties_keep_first_seen_order() -> None:
    assert [key for key, _ in reciprocal_rank_fusion([["a"], ["b"]])] == ["a", "b"]


# Search and fallback ----------------------------------------------------------------

def test_search_uses_hybrid_when_available(calls) -> None:
    matches = rag.search(object(), "BitLocker key needed")
    assert calls == [("hybrid", "BitLocker key needed")]
    assert matches == [KbMatch(ROW["slug"], ROW["title"], ROW["category"], ROW["content"], 0.64, "hybrid")]


def test_query_text_is_truncated_like_the_embedding_input(calls) -> None:
    rag.search(object(), "x" * 5000)
    assert len(calls[0][1]) == rag.QUERY_CHAR_LIMIT


@pytest.mark.parametrize("error", [errors.UndefinedFunction, errors.UndefinedColumn, errors.InsufficientPrivilege])
def test_missing_migration_falls_back_to_vector_and_pauses_hybrid(calls, monkeypatch, error) -> None:
    def missing(*args):
        calls.append(("hybrid", "failed"))
        raise error("function match_kb_hybrid does not exist")

    monkeypatch.setattr(store, "match_kb_hybrid", missing)
    first = rag.search(object(), "ticket text")
    second = rag.search(object(), "ticket text")
    assert [m.retrieval for m in first + second] == ["vector", "vector"]
    # The second search skips the failing call while the switch is off.
    assert calls == [("hybrid", "failed"), ("vector", 3), ("vector", 3)]
    assert not hybrid.switch.enabled()


def test_other_database_errors_fall_back_once_without_pausing(calls, monkeypatch) -> None:
    def timeout(*args):
        raise errors.QueryCanceled("statement timeout")

    monkeypatch.setattr(store, "match_kb_hybrid", timeout)
    assert [m.retrieval for m in rag.search(object(), "ticket")] == ["vector"]
    assert hybrid.switch.enabled()


def test_null_similarity_from_a_text_only_hit_is_zero(calls, monkeypatch) -> None:
    monkeypatch.setattr(store, "match_kb_hybrid", lambda *args: [{**ROW, "similarity": None}])
    assert rag.search(object(), "ticket")[0].similarity == 0.0


def test_a_vector_failure_still_reaches_the_caller(calls, monkeypatch) -> None:
    hybrid.switch.disable("test")
    monkeypatch.setattr(store, "match_kb", lambda *args: (_ for _ in ()).throw(psycopg.OperationalError("down")))
    with pytest.raises(psycopg.OperationalError):
        rag.search(object(), "ticket")


def test_switch_retries_after_the_window() -> None:
    now = [1000.0]
    switch = HybridSwitch(retry_after=300, clock=lambda: now[0])
    switch.disable("UndefinedFunction")
    assert not switch.enabled()
    now[0] += 299
    assert not switch.enabled()
    now[0] += 1
    assert switch.enabled()


def test_switch_logs_once_per_outage(caplog: pytest.LogCaptureFixture) -> None:
    switch = HybridSwitch(clock=lambda: 0.0)
    with caplog.at_level("WARNING", logger="triageops.hybrid"):
        switch.disable("UndefinedFunction")
        switch.disable("UndefinedFunction")
    assert len(caplog.records) == 1


# Callers record which retrieval ran ------------------------------------------------

def test_triage_records_the_retrieval_method(fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    match = KbMatch("vpn-error-809", "VPN error 809", "Network", "1. Restart the router.", 0.8, "hybrid")
    monkeypatch.setattr(rag, "search", lambda conn, text, count=3: [match])
    sample = SAMPLES_BY_KEY["printer"]
    inbound = InboundTicket(channel="portal", subject=sample.subject, body=sample.body)
    bundle = triage.process_ticket(object(), inbound)
    detail = next(e["detail"] for e in fake_store.events if e["type"] == "kb_matched")
    assert detail["method"] == "hybrid"
    assert detail["matches"][0]["slug"] == "vpn-error-809"
    assert bundle.ticket["kb_refs"][0]["slug"] == "vpn-error-809"


def test_copilot_tool_reports_hybrid_search(monkeypatch: pytest.MonkeyPatch) -> None:
    match = KbMatch("vpn-error-809", "VPN error 809", "Network", "1. Restart the router.", 0.8, "hybrid")
    monkeypatch.setattr(rag, "search", lambda conn, text, count=3: [match])
    result = tools._search_knowledge_base(object(), tools.SearchKnowledgeBaseArguments(query="vpn 809", limit=3))
    assert result["method"] == "hybrid"
