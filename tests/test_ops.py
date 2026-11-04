"""Operations metrics: pricing, percentiles, daily buckets, model mix, fallback rate, SLA and the endpoint."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fakes import FakeStore
from fastapi.testclient import TestClient

from triageops import ops, store
from triageops.app import app
from triageops.ops import (
    FALLBACK_MODEL,
    PRICING,
    Window,
    compute_metrics,
    percentile,
    price_for,
)
from triageops.triage import FALLBACK_MODEL as TRIAGE_FALLBACK_MODEL

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


def row(day_offset: int = 0, **fields: Any) -> dict[str, Any]:
    base = {
        "created_at": NOW - timedelta(days=day_offset),
        "ai_model": "claude-haiku-5-5",
        "ai_latency_ms": 1000,
        "input_tokens": 1000,
        "output_tokens": 200,
        "priority": "P3",
        "sla_due_at": NOW + timedelta(hours=24),
        "sla_breached": False,
    }
    return {**base, **fields}


def metrics(rows: list[dict[str, Any]], days: int = 7) -> dict[str, Any]:
    return compute_metrics(rows, Window.ending(NOW, days), now=NOW)


# Building blocks ------------------------------------------------------------------

def test_fallback_label_matches_the_pipeline() -> None:
    assert FALLBACK_MODEL == TRIAGE_FALLBACK_MODEL


def test_price_lookup_handles_exact_dated_and_unknown_models() -> None:
    assert price_for("claude-haiku-5-5") is PRICING["claude-haiku-5-5"]
    assert price_for("claude-sonnet-5-5-20261101") is PRICING["claude-sonnet-5-5"]
    assert price_for("rules-fallback") is None
    assert price_for(None) is None


def test_haiku_cost_for_a_typical_triage_call() -> None:
    # 1,200 input and 310 output tokens at $0.10 and $0.50 per million.
    assert PRICING["claude-haiku-5-5"].cost(1200, 310) == pytest.approx(0.000275)


@pytest.mark.parametrize(
    ("values", "fraction", "expected"),
    [
        ([], 0.5, None),
        ([7], 0.95, 7.0),
        ([1, 2, 3, 4], 0.5, 2.5),
        ([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 0.95, 95.5),
        ([5, 1, 3], 0.5, 3.0),
    ],
)
def test_percentile_interpolates_like_percentile_cont(values, fraction, expected) -> None:
    assert percentile(values, fraction) == (None if expected is None else pytest.approx(expected))


def test_window_covers_whole_london_days() -> None:
    window = Window.ending(NOW, 7)
    assert (window.first_day, window.last_day) == (date(2026, 10, 29), date(2026, 11, 4))
    assert window.start_utc == datetime(2026, 10, 29, 0, 0, tzinfo=UTC)
    assert len(window.dates()) == 7


def test_window_start_follows_british_summer_time() -> None:
    window = Window.ending(datetime(2026, 7, 10, 12, 0, tzinfo=UTC), 1)
    assert window.start_utc == datetime(2026, 7, 9, 23, 0, tzinfo=UTC)


def test_late_evening_utc_in_summer_counts_as_the_next_london_day() -> None:
    summer = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    late = row(ai_model="claude-haiku-5-5", created_at=datetime(2026, 7, 9, 23, 30, tzinfo=UTC))
    result = compute_metrics([late], Window.ending(summer, 2), now=summer)
    assert [d["tickets"] for d in result["daily"]] == [0, 1]


# Metrics --------------------------------------------------------------------------

def test_empty_window_returns_zeros_and_nulls() -> None:
    result = metrics([])
    assert result["totals"]["tickets"] == 0
    assert result["totals"]["cost_per_ticket_usd"] is None
    assert result["latency_ms"] == {"p50": None, "p95": None, "samples": 0}
    assert result["fallback_rate"] is None
    assert result["sla"]["breach_rate"] is None
    assert len(result["daily"]) == 7
    assert all(day["tickets"] == 0 for day in result["daily"])


def test_cost_tokens_and_cost_per_ticket() -> None:
    rows = [row(), row(), row(ai_model="claude-sonnet-5-5", input_tokens=2000, output_tokens=500)]
    totals = metrics(rows)["totals"]
    haiku = (1000 * 0.10 + 200 * 0.50) / 1_000_000
    sonnet = (2000 * 2.00 + 500 * 10.00) / 1_000_000
    assert totals["input_tokens"] == 4000
    assert totals["output_tokens"] == 900
    assert totals["cost_usd"] == pytest.approx(2 * haiku + sonnet)
    assert totals["cost_per_ticket_usd"] == pytest.approx((2 * haiku + sonnet) / 3, abs=1e-8)


def test_latency_percentiles_exclude_fallback_and_untriaged_tickets() -> None:
    rows = [row(ai_latency_ms=ms) for ms in (800, 1200, 1600, 4000)]
    rows += [row(ai_model=FALLBACK_MODEL, ai_latency_ms=2), row(ai_model=None, ai_latency_ms=None)]
    latency = metrics(rows)["latency_ms"]
    assert latency["samples"] == 4
    assert latency["p50"] == 1400.0
    assert latency["p95"] == pytest.approx(3640.0)


def test_fallback_rate_and_model_mix() -> None:
    rows = [row(), row(), row(), row(ai_model=FALLBACK_MODEL, input_tokens=None, output_tokens=None), row(ai_model=None)]
    result = metrics(rows)
    assert result["totals"]["ai_attempted"] == 4
    assert result["fallback_rate"] == 0.25
    assert [(m["model"], m["tickets"], m["share"]) for m in result["model_mix"]] == [
        ("claude-haiku-5-5", 3, 0.75),
        (FALLBACK_MODEL, 1, 0.25),
    ]
    fallback = result["model_mix"][1]
    assert fallback["cost_usd"] == 0
    assert fallback["p50_ms"] is None


def test_unknown_models_are_counted_but_not_priced() -> None:
    totals = metrics([row(ai_model="some-other-model")])["totals"]
    assert totals["unpriced_tickets"] == 1
    assert totals["cost_usd"] == 0


def test_sla_breaches_by_priority_and_rate() -> None:
    rows = [
        row(priority="P1", sla_breached=True),
        row(priority="P2", sla_breached=True),
        row(priority="P2"),
        row(priority="P3", sla_due_at=None),
    ]
    sla = metrics(rows)["sla"]
    assert sla["breached"] == 2
    assert sla["with_sla"] == 3
    assert sla["breach_rate"] == pytest.approx(0.6667)
    assert sla["by_priority"] == {"P1": 1, "P2": 1, "P3": 0, "P4": 0}


def test_daily_series_buckets_by_day_and_ignores_rows_outside_the_window() -> None:
    rows = [row(0), row(0, sla_breached=True), row(2, ai_model=FALLBACK_MODEL), row(30)]
    result = metrics(rows)
    by_date = {day["date"]: day for day in result["daily"]}
    assert list(by_date) == [f"2026-10-{d}" for d in (29, 30, 31)] + [f"2026-11-0{d}" for d in range(1, 5)]
    assert by_date["2026-11-04"]["tickets"] == 2
    assert by_date["2026-11-04"]["sla_breached"] == 1
    assert by_date["2026-11-04"]["p95_ms"] == 1000.0
    assert by_date["2026-11-02"]["fallback"] == 1
    assert result["totals"]["tickets"] == 3


# Endpoint -------------------------------------------------------------------------

@pytest.fixture
def client(fake_store: FakeStore) -> TestClient:
    return TestClient(app)


def test_endpoint_reads_the_window_and_returns_metrics(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def rows(conn, since, limit):
        seen.update(since=since, limit=limit)
        return [row(0, created_at=datetime.now(UTC))]

    monkeypatch.setattr(store, "ops_ticket_rows", rows)
    response = client.get("/api/ops/metrics?days=7")
    assert response.status_code == 200
    body = response.json()
    assert body["window"]["days"] == 7
    assert body["window"]["timezone"] == "Europe/London"
    assert body["totals"]["tickets"] == 1
    assert body["currency"] == "USD"
    assert body["truncated"] is False
    assert seen["limit"] == ops.ROW_LIMIT + 1
    assert datetime.now(UTC) - seen["since"] < timedelta(days=8)
    assert response.headers["x-content-type-options"] == "nosniff"


def test_endpoint_defaults_to_fourteen_days(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "ops_ticket_rows", lambda conn, since, limit: [])
    assert client.get("/api/ops/metrics").json()["window"]["days"] == 14


@pytest.mark.parametrize("days", ["0", "91", "lots"])
def test_endpoint_rejects_bad_windows(client: TestClient, days: str) -> None:
    response = client.get(f"/api/ops/metrics?days={days}")
    assert response.status_code == 422
    assert response.json()["error"].startswith("Check the days field")


def test_endpoint_flags_a_truncated_read(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops, "ROW_LIMIT", 2)
    monkeypatch.setattr(store, "ops_ticket_rows", lambda conn, since, limit: [row(0, created_at=datetime.now(UTC))] * limit)
    body = client.get("/api/ops/metrics").json()
    assert body["truncated"] is True
    assert body["totals"]["tickets"] == 2


def test_endpoint_is_503_when_the_database_is_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from triageops import db
    from triageops.db import DatabaseUnavailable

    def down():
        raise DatabaseUnavailable("no database URL accepted a connection")

    monkeypatch.setattr(db, "connect", down)
    response = client.get("/api/ops/metrics")
    assert response.status_code == 503
