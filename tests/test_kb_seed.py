"""Knowledge base seeding: idempotent, two texts per embedding call, time budget, partial failure."""

from contextlib import contextmanager
from typing import Any

import pytest
from fakes import FakeStore
from fastapi.testclient import TestClient

from triageops import db, kb_seed, rag, store
from triageops.app import app
from triageops.config import settings
from triageops.kb_seed import KB_ARTICLES, SEED_BATCH_SIZE
from triageops.rag import EmbeddingError


class SeedConn:
    """Just enough connection for seed_kb: a transaction block that records commits."""

    def __init__(self) -> None:
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


class FakeKb:
    def __init__(self, existing: set[str] | None = None, locked: bool = True) -> None:
        self.slugs = set(existing or ())
        self.locked = locked
        self.embed_batches: list[list[str]] = []
        self.inserted_batches: list[list[str]] = []
        self.fail_on_batch: int | None = None

    def embed(self, texts, timeout=0.0):
        self.embed_batches.append(list(texts))
        if self.fail_on_batch is not None and len(self.embed_batches) == self.fail_on_batch:
            raise EmbeddingError("embedding request failed (ReadTimeout)")
        return [[0.0] * rag.EMBEDDING_DIMENSIONS for _ in texts]

    def insert(self, conn, rows) -> int:
        new = [row[0] for row in rows if row[0] not in self.slugs]
        self.inserted_batches.append([row[0] for row in rows])
        self.slugs.update(new)
        return len(new)


@pytest.fixture
def kb(monkeypatch: pytest.MonkeyPatch) -> FakeKb:
    fake = FakeKb()
    monkeypatch.setattr(rag, "embed", fake.embed)
    monkeypatch.setattr(store, "insert_kb_articles", fake.insert)
    monkeypatch.setattr(store, "existing_kb_slugs", lambda conn: set(fake.slugs))
    monkeypatch.setattr(store, "try_advisory_xact_lock", lambda conn, key: fake.locked)
    kb_seed.reset_cooldown()
    yield fake
    kb_seed.reset_cooldown()


def test_empty_database_gets_all_thirty_articles(kb: FakeKb) -> None:
    assert kb_seed.seed_kb(SeedConn()) == 30
    assert kb.slugs == {article.slug for article in KB_ARTICLES}


def test_every_embedding_call_has_at_most_two_texts(kb: FakeKb) -> None:
    kb_seed.seed_kb(SeedConn())
    assert SEED_BATCH_SIZE == 2
    assert all(1 <= len(batch) <= 2 for batch in kb.embed_batches)
    assert len(kb.embed_batches) == 15


def test_existing_twelve_are_skipped_and_eighteen_added(kb: FakeKb) -> None:
    kb.slugs = {article.slug for article in KB_ARTICLES[:12]}
    assert kb_seed.seed_kb(SeedConn()) == 18
    embedded = [text.split("\n", 1)[0] for batch in kb.embed_batches for text in batch]
    assert embedded == [article.title for article in KB_ARTICLES[12:]]


def test_second_run_is_a_no_op(kb: FakeKb) -> None:
    kb_seed.seed_kb(SeedConn())
    calls = len(kb.embed_batches)
    assert kb_seed.seed_kb(SeedConn()) == 0
    assert len(kb.embed_batches) == calls


def test_another_instance_holding_the_lock_means_no_work(kb: FakeKb) -> None:
    kb.locked = False
    assert kb_seed.seed_kb(SeedConn()) == 0
    assert kb.embed_batches == []


def test_time_budget_stops_new_batches_and_keeps_progress(kb: FakeKb) -> None:
    ticks = iter(range(100))
    inserted = kb_seed.seed_kb(SeedConn(), time_budget=2.5, clock=lambda: float(next(ticks)))
    # The deadline is 0 + 2.5. The first batch always runs, batches two and three read 1 and 2,
    # and batch four reads 3, which is past the deadline.
    assert inserted == 6
    assert kb_seed.seed_kb(SeedConn()) == 24


def test_partial_failure_inserts_completed_batches(kb: FakeKb) -> None:
    kb.fail_on_batch = 3
    assert kb_seed.seed_kb(SeedConn()) == 4
    kb.fail_on_batch = None
    assert kb_seed.seed_kb(SeedConn()) == 26


def test_failure_on_the_first_batch_raises(kb: FakeKb) -> None:
    kb.fail_on_batch = 1
    with pytest.raises(EmbeddingError):
        kb_seed.seed_kb(SeedConn())
    assert kb.inserted_batches == []


def test_health_seeding_waits_after_a_failure(kb: FakeKb) -> None:
    now = [0.0]
    kb.fail_on_batch = 1
    with pytest.raises(EmbeddingError):
        kb_seed.seed_if_due(SeedConn(), clock=lambda: now[0])
    kb.fail_on_batch = None
    now[0] += 60
    assert kb_seed.seed_if_due(SeedConn(), clock=lambda: now[0]) == 0
    assert len(kb.embed_batches) == 1
    now[0] += kb_seed.FAILURE_COOLDOWN_SECONDS
    assert kb_seed.seed_if_due(SeedConn(), clock=lambda: now[0]) == 30


def test_health_tops_up_a_database_seeded_with_the_original_twelve(
    fake_store: FakeStore, kb: FakeKb, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb.slugs = {article.slug for article in KB_ARTICLES[:12]}
    monkeypatch.setattr(settings, "embed_secret", "test-embed-secret")
    monkeypatch.setattr(store, "kb_count", lambda conn: len(kb.slugs))

    @contextmanager
    def connect() -> Any:
        yield SeedConn()

    monkeypatch.setattr(db, "connect", connect)
    response = TestClient(app).get("/api/health")
    assert response.json()["kb_articles"] == 30


def test_health_reports_the_articles_it_has_when_a_top_up_fails(
    fake_store: FakeStore, kb: FakeKb, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb.slugs = {article.slug for article in KB_ARTICLES[:12]}
    kb.fail_on_batch = 1
    monkeypatch.setattr(settings, "embed_secret", "test-embed-secret")
    monkeypatch.setattr(store, "kb_count", lambda conn: len(kb.slugs))

    @contextmanager
    def connect() -> Any:
        yield SeedConn()

    monkeypatch.setattr(db, "connect", connect)
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json()["kb_articles"] == 12


def test_health_does_not_seed_a_full_knowledge_base(fake_store: FakeStore, kb: FakeKb, monkeypatch) -> None:
    monkeypatch.setattr(settings, "embed_secret", "test-embed-secret")
    fake_store.kb_articles = 30
    assert TestClient(app).get("/api/health").json()["kb_articles"] == 30
    assert kb.embed_batches == []
