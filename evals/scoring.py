"""Score triage results against the labelled dataset. Pure functions, no I/O.

A result is one dict per ticket, produced by ``harness.run_case``:

``{"id", "subject", "channel", "tags", "rationale", "expected": {...}, "actual": {...} or None,
"model_proposed": {...} or None, "rules_fired": [...], "model", "fallback", "fallback_reason",
"latency_ms", "pipeline_ms", "input_tokens", "output_tokens", "error"}``

``expected`` and ``actual`` hold ``category``, ``priority``, ``team`` and
``injection``. ``actual`` is ``None`` when the pipeline raised, and such a
ticket counts as wrong on every measure, including P1 recall.
"""

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from triageops.taxonomy import PRIORITIES

from .pricing import price_for

SCHEMA_VERSION = 1
P1_RECALL_TARGET = 1.0
FIELDS = ("priority", "category", "team", "injection")

Result = dict[str, Any]


def ratio(numerator: int, denominator: int) -> float | None:
    """A rate rounded to four places, or ``None`` when there is nothing to divide by."""
    return round(numerator / denominator, 4) if denominator else None


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest rank percentile, so p95 of 60 values is the 57th smallest."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


def distribution(values: Iterable[float | int | None]) -> dict[str, Any]:
    present = [float(v) for v in values if v is not None]
    if not present:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(present),
        "mean": round(sum(present) / len(present), 1),
        "p50": percentile(present, 50),
        "p95": percentile(present, 95),
        "max": max(present),
    }


def _actual(result: Result, field: str) -> Any:
    actual = result.get("actual")
    return None if actual is None else actual.get(field)


def mismatches(result: Result) -> list[str]:
    """The fields where the final triage differs from the label, in a fixed order."""
    return [field for field in FIELDS if _actual(result, field) != result["expected"][field]]


def score_p1(results: Sequence[Result]) -> dict[str, Any]:
    expected = [r for r in results if r["expected"]["priority"] == "P1"]
    predicted = [r for r in results if _actual(r, "priority") == "P1"]
    caught = [r for r in expected if _actual(r, "priority") == "P1"]
    return {
        "recall": ratio(len(caught), len(expected)),
        "precision": ratio(len(caught), len(predicted)),
        "expected": len(expected),
        "caught": len(caught),
        "missed": [r["id"] for r in expected if _actual(r, "priority") != "P1"],
        "false_alarms": [r["id"] for r in predicted if r["expected"]["priority"] != "P1"],
    }


def score_priority(results: Sequence[Result]) -> dict[str, Any]:
    """Accuracy plus a confusion matrix with expected priority as rows and predicted as columns.

    A ticket with no prediction lands in the ``none`` column, so every row still
    sums to the number of tickets with that label.
    """
    labels = list(PRIORITIES)
    columns = [*labels, "none"]
    matrix = [[0] * len(columns) for _ in labels]
    for result in results:
        row = labels.index(result["expected"]["priority"])
        predicted = _actual(result, "priority")
        matrix[row][columns.index(predicted if predicted in labels else "none")] += 1

    per_class = {}
    for i, label in enumerate(labels):
        support = sum(matrix[i])
        predicted_total = sum(matrix[r][i] for r in range(len(labels)))
        per_class[label] = {
            "support": support,
            "recall": ratio(matrix[i][i], support),
            "precision": ratio(matrix[i][i], predicted_total),
        }

    correct = sum(matrix[i][i] for i in range(len(labels)))
    # Lower number means more severe, so P1 predicted as P3 is under triage.
    under = sum(matrix[r][c] for r in range(len(labels)) for c in range(len(labels)) if c > r)
    over = sum(matrix[r][c] for r in range(len(labels)) for c in range(len(labels)) if c < r)
    return {
        "accuracy": ratio(correct, len(results)),
        "correct": correct,
        "total": len(results),
        "labels": labels,
        "columns": columns,
        "confusion": matrix,
        "per_class": per_class,
        "under_triaged": under,
        "over_triaged": over,
        "unscored": sum(row[-1] for row in matrix),
    }


def score_field(results: Sequence[Result], field: str) -> dict[str, Any]:
    correct = sum(1 for r in results if _actual(r, field) == r["expected"][field])
    by_label: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_label.setdefault(r["expected"][field], {"support": 0, "correct": 0})
        bucket["support"] += 1
        bucket["correct"] += int(_actual(r, field) == r["expected"][field])
    return {
        "accuracy": ratio(correct, len(results)),
        "correct": correct,
        "total": len(results),
        "per_label": dict(sorted(by_label.items())),
    }


def score_injection(results: Sequence[Result]) -> dict[str, Any]:
    positives = [r for r in results if r["expected"]["injection"]]
    negatives = [r for r in results if not r["expected"]["injection"]]
    detected = [r for r in positives if _actual(r, "injection") is True]
    false_positives = [r for r in negatives if _actual(r, "injection") is True]
    return {
        "detection_rate": ratio(len(detected), len(positives)),
        "detected": len(detected),
        "expected": len(positives),
        "missed": [r["id"] for r in positives if _actual(r, "injection") is not True],
        "false_positives": len(false_positives),
        "false_positive_rate": ratio(len(false_positives), len(negatives)),
        "negatives": len(negatives),
        "false_positive_ids": [r["id"] for r in false_positives],
    }


def score_usage(results: Sequence[Result], model: str | None) -> dict[str, Any]:
    """Tokens and cost over the tickets that Claude actually triaged."""
    billed = [r for r in results if r.get("input_tokens") is not None or r.get("output_tokens") is not None]
    input_total = sum(r.get("input_tokens") or 0 for r in billed)
    output_total = sum(r.get("output_tokens") or 0 for r in billed)
    price = price_for(model) or price_for(next((r.get("model") for r in billed), None))
    cost_total = price.cost(input_total, output_total) if price and billed else None
    return {
        "tokens": {
            "billed_tickets": len(billed),
            "input_total": input_total,
            "output_total": output_total,
            "input_mean": round(input_total / len(billed), 1) if billed else None,
            "output_mean": round(output_total / len(billed), 1) if billed else None,
        },
        "cost_usd": {
            "total": round(cost_total, 6) if cost_total is not None else None,
            "per_ticket": round(cost_total / len(billed), 6) if cost_total is not None else None,
            "per_1000_tickets": round(cost_total / len(billed) * 1000, 4) if cost_total is not None else None,
            "input_per_mtok": price.input_per_mtok if price else None,
            "output_per_mtok": price.output_per_mtok if price else None,
        },
    }


def failure(result: Result) -> dict[str, Any]:
    return {
        "id": result["id"],
        "subject": result["subject"],
        "channel": result.get("channel"),
        "tags": result.get("tags", []),
        "rationale": result.get("rationale", ""),
        "mismatches": mismatches(result),
        "expected": result["expected"],
        "actual": result.get("actual"),
        "model_proposed": result.get("model_proposed"),
        "rules_fired": result.get("rules_fired", []),
        "fallback": bool(result.get("fallback")),
        "error": result.get("error"),
    }


def _severity(item: dict[str, Any]) -> tuple[int, int, str]:
    """Missed P1s first, then other priority errors, then everything else."""
    missed_p1 = item["expected"]["priority"] == "P1" and "priority" in item["mismatches"]
    return (0 if missed_p1 else 1, 0 if "priority" in item["mismatches"] else 1, item["id"])


def score(results: Sequence[Result], *, model: str | None = None) -> dict[str, Any]:
    """Every metric for one run, plus the release gate and the list of failures."""
    ok = [r for r in results if r.get("actual") is not None]
    claude = [r for r in ok if not r.get("fallback")]
    p1 = score_p1(results)
    usage = score_usage(results, model)
    rules = Counter(rule for r in ok for rule in r.get("rules_fired", []))
    gate_value = p1["recall"]
    failures = sorted((failure(r) for r in results if mismatches(r)), key=_severity)
    return {
        "gate": {
            "metric": "p1_recall",
            "target": P1_RECALL_TARGET,
            "value": gate_value,
            "passed": gate_value is not None and gate_value >= P1_RECALL_TARGET,
        },
        "metrics": {
            "tickets": len(results),
            "p1": p1,
            "priority": score_priority(results),
            "category": score_field(results, "category"),
            "team": score_field(results, "team"),
            "injection": score_injection(results),
            # Pipeline time is end to end per ticket. Model time covers only tickets Claude triaged.
            "latency_ms": distribution(r.get("pipeline_ms") for r in ok),
            "model_latency_ms": distribution(r.get("latency_ms") for r in claude),
            **usage,
            "fallbacks": {
                "count": sum(1 for r in ok if r.get("fallback")),
                "ids": [r["id"] for r in ok if r.get("fallback")],
                "reasons": dict(Counter(r.get("fallback_reason") or "unknown" for r in ok if r.get("fallback"))),
            },
            "errors": {"count": len(results) - len(ok), "ids": [r["id"] for r in results if r.get("actual") is None]},
            "rules_fired": dict(sorted(rules.items())),
            "fully_correct": sum(1 for r in results if not mismatches(r)),
        },
        "failures": failures,
    }
