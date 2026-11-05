"""Operations metrics for the /ops page, computed from columns the tickets table already has.

``GET /api/ops/metrics`` reads one narrow row per ticket in the window and works out
cost, tokens, latency percentiles, model mix, fallback rate and SLA breaches in
Python. That keeps every number unit tested without a database. At demo volumes
(a few hundred tickets a day) the read is small, and a row cap protects the
function if volumes ever grow. The next step at real scale would be moving the
aggregation into SQL with ``percentile_cont``, which uses the same interpolation
as ``percentile`` below, so the numbers would not change.

Cost is an estimate: token counts times list prices. Only triage calls are
recorded per ticket, so copilot chat spend is not included.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query

from . import db, store
from .taxonomy import PRIORITIES

FALLBACK_MODEL = "rules-fallback"  # Same value as triage.FALLBACK_MODEL.
DEFAULT_DAYS = 14
MAX_DAYS = 90
ROW_LIMIT = 50_000

try:
    REPORT_TZ = ZoneInfo("Europe/London")
except ZoneInfoNotFoundError:  # pragma: no cover, only on hosts without a time zone database
    REPORT_TZ = ZoneInfo("UTC")


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens, at list price."""

    input_per_million: float
    output_per_million: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_million + output_tokens * self.output_per_million) / 1_000_000


# Anthropic list prices per million tokens. Haiku 5.5 is priced for prompts up to 100K
# tokens, which covers every triage call. Check these against the pricing page when
# models or prices change.
PRICING: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.10, 0.50),
    "claude-sonnet-5-5": Price(2.00, 10.00),
    "claude-opus-5-5": Price(4.00, 20.00),
    "claude-fable-5-1": Price(10.00, 50.00),
}


def price_for(model: str | None) -> Price | None:
    """Exact id first, then the longest known id the model starts with (for dated snapshots)."""
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    matches = [key for key in PRICING if model.startswith(key)]
    return PRICING[max(matches, key=len)] if matches else None


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Linear interpolation between closest ranks, like Postgres ``percentile_cont``."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass(frozen=True)
class Window:
    days: int
    first_day: date
    last_day: date

    @classmethod
    def ending(cls, now: datetime, days: int) -> "Window":
        last_day = now.astimezone(REPORT_TZ).date()
        return cls(days=days, first_day=last_day - timedelta(days=days - 1), last_day=last_day)

    @property
    def start_utc(self) -> datetime:
        return datetime.combine(self.first_day, clock_time(0), tzinfo=REPORT_TZ).astimezone(UTC)

    def dates(self) -> list[date]:
        return [self.first_day + timedelta(days=offset) for offset in range(self.days)]


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


class _Bucket:
    """Running totals for one day or one model."""

    def __init__(self) -> None:
        self.tickets = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        self.latencies: list[float] = []
        self.fallback = 0
        self.sla_breached = 0

    def add(self, row: Mapping[str, Any], cost: float | None) -> None:
        self.tickets += 1
        self.input_tokens += row.get("input_tokens") or 0
        self.output_tokens += row.get("output_tokens") or 0
        self.cost += cost or 0.0
        if row.get("ai_model") == FALLBACK_MODEL:
            self.fallback += 1
        elif row.get("ai_model") and row.get("ai_latency_ms") is not None:
            self.latencies.append(float(row["ai_latency_ms"]))
        if row.get("sla_breached"):
            self.sla_breached += 1


def compute_metrics(
    rows: Iterable[Mapping[str, Any]],
    window: Window,
    *,
    now: datetime,
    truncated: bool = False,
) -> dict[str, Any]:
    """Every figure the /ops page shows. ``rows`` come from ``store.ops_ticket_rows``."""
    days = {day: _Bucket() for day in window.dates()}
    models: dict[str, _Bucket] = {}
    total = _Bucket()
    attempted = 0
    unpriced = 0
    with_sla = 0
    breached_by_priority = dict.fromkeys(PRIORITIES, 0)

    for row in rows:
        day = row["created_at"].astimezone(REPORT_TZ).date()
        if day not in days:
            continue
        model = row.get("ai_model")
        cost = None
        if model and model != FALLBACK_MODEL:
            price = price_for(model)
            if price is None:
                unpriced += 1
            else:
                cost = price.cost(row.get("input_tokens") or 0, row.get("output_tokens") or 0)
        total.add(row, cost)
        days[day].add(row, cost)
        if model:
            attempted += 1
            models.setdefault(model, _Bucket()).add(row, cost)
        if row.get("sla_due_at") is not None:
            with_sla += 1
        if row.get("sla_breached") and row.get("priority") in breached_by_priority:
            breached_by_priority[row["priority"]] += 1

    model_mix = [
        {
            "model": name,
            "tickets": bucket.tickets,
            "share": _ratio(bucket.tickets, attempted),
            "input_tokens": bucket.input_tokens,
            "output_tokens": bucket.output_tokens,
            "cost_usd": round(bucket.cost, 8),
            "p50_ms": _rounded(percentile(bucket.latencies, 0.5), 1),
            "p95_ms": _rounded(percentile(bucket.latencies, 0.95), 1),
        }
        for name, bucket in sorted(models.items(), key=lambda item: (-item[1].tickets, item[0]))
    ]

    return {
        "generated_at": now.astimezone(UTC).isoformat(),
        "window": {
            "days": window.days,
            "from": window.first_day.isoformat(),
            "to": window.last_day.isoformat(),
            "timezone": str(REPORT_TZ),
        },
        "currency": "USD",
        "pricing": {
            name: {"input_per_million": price.input_per_million, "output_per_million": price.output_per_million}
            for name, price in PRICING.items()
        },
        "totals": {
            "tickets": total.tickets,
            "ai_attempted": attempted,
            "fallback": total.fallback,
            "input_tokens": total.input_tokens,
            "output_tokens": total.output_tokens,
            "cost_usd": round(total.cost, 8),
            "cost_per_ticket_usd": round(total.cost / attempted, 8) if attempted else None,
            "unpriced_tickets": unpriced,
        },
        "latency_ms": {
            "p50": _rounded(percentile(total.latencies, 0.5), 1),
            "p95": _rounded(percentile(total.latencies, 0.95), 1),
            "samples": len(total.latencies),
        },
        "fallback_rate": _ratio(total.fallback, attempted),
        "model_mix": model_mix,
        "sla": {
            "breached": total.sla_breached,
            "with_sla": with_sla,
            "breach_rate": _ratio(total.sla_breached, with_sla),
            "by_priority": breached_by_priority,
        },
        "daily": [
            {
                "date": day.isoformat(),
                "tickets": bucket.tickets,
                "input_tokens": bucket.input_tokens,
                "output_tokens": bucket.output_tokens,
                "cost_usd": round(bucket.cost, 8),
                "p50_ms": _rounded(percentile(bucket.latencies, 0.5), 1),
                "p95_ms": _rounded(percentile(bucket.latencies, 0.95), 1),
                "fallback": bucket.fallback,
                "sla_breached": bucket.sla_breached,
            }
            for day, bucket in days.items()
        ],
        "truncated": truncated,
    }


router = APIRouter()


@router.get("/api/ops/metrics")
def ops_metrics(days: int = Query(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS)) -> dict[str, Any]:
    now = datetime.now(UTC)
    window = Window.ending(now, days)
    with db.connect() as conn:
        rows = store.ops_ticket_rows(conn, window.start_utc, ROW_LIMIT + 1)
    return compute_metrics(rows[:ROW_LIMIT], window, now=now, truncated=len(rows) > ROW_LIMIT)
