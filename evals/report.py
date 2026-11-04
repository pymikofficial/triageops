"""Render a scored run as Markdown, for the repository and the GitHub Actions job summary."""

from typing import Any


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.0f} ms"


def usd(value: float | None, places: int = 5) -> str:
    return "n/a" if value is None else f"${value:.{places}f}"


def tickets(count: int) -> str:
    return f"{count} ticket{'' if count == 1 else 's'}"


def _ids(ids: list[str]) -> str:
    return ", ".join(ids) if ids else "none"


def _row(cells: list[Any]) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _label(field: str, values: dict[str, Any] | None) -> str:
    if values is None:
        return "no result"
    value = values.get(field)
    if field == "injection":
        return "flagged" if value else "not flagged"
    return str(value)


def render(report: dict[str, Any]) -> str:
    run, m, gate = report["run"], report["metrics"], report["gate"]
    p1, priority, injection = m["p1"], m["priority"], m["injection"]
    lines = [
        "# TriageOps evaluation results",
        "",
        f"Generated {run['generated_at']} by `{run['model']}`"
        + (" (the keyword fallback, Claude was not called)" if run["mode"] == "rules-fallback" else "")
        + f", {m['tickets']} of {run['dataset']['tickets']} tickets from `{run['dataset']['path']}`, "
        f"prompt `{run['prompt_sha256']}`.",
        "",
    ]
    if run["mode"] == "rules-fallback":
        lines += [
            "> These numbers come from the keyword fallback, not from Claude. No API key was used for this run. "
            "Run the Evals workflow, or `python evals/run_eval.py` with `ANTHROPIC_API_KEY` set, to measure Claude.",
            "",
        ]
    if m["fallbacks"]["count"] and run["mode"] != "rules-fallback":
        lines += [
            f"> {tickets(m['fallbacks']['count'])} fell back to keyword rules because the Claude call failed "
            f"({_ids(m['fallbacks']['ids'])}). Reasons: "
            + ", ".join(f"{reason} ({count})" for reason, count in m["fallbacks"]["reasons"].items())
            + ".",
            "",
        ]
    verdict = "PASSED" if gate["passed"] else "FAILED"
    lines += [
        f"## Release gate: {verdict}",
        "",
        f"P1 recall is {pct(gate['value'])} against a target of {pct(gate['target'])}. "
        f"{p1['caught']} of {p1['expected']} P1 tickets were triaged as P1. Missed: {_ids(p1['missed'])}.",
        "",
        "## Summary",
        "",
        _row(["Measure", "Result", "Detail"]),
        _row(["---", "---", "---"]),
        _row(["P1 recall", pct(p1["recall"]), f"{p1['caught']} of {p1['expected']}, precision {pct(p1['precision'])}"]),
        _row([
            "Priority accuracy", pct(priority["accuracy"]),
            f"{priority['correct']} of {priority['total']}, {priority['under_triaged']} under triaged, "
            f"{priority['over_triaged']} over triaged",
        ]),
        _row(["Category accuracy", pct(m["category"]["accuracy"]), f"{m['category']['correct']} of {m['category']['total']}"]),
        _row(["Team accuracy", pct(m["team"]["accuracy"]), f"{m['team']['correct']} of {m['team']['total']}"]),
        _row([
            "Injection detection", pct(injection["detection_rate"]),
            f"{injection['detected']} of {injection['expected']} caught",
        ]),
        _row([
            "Injection false positives", str(injection["false_positives"]),
            f"{pct(injection['false_positive_rate'])} of {injection['negatives']} clean tickets",
        ]),
        _row(["Latency, mean", ms(m["latency_ms"]["mean"]), "End to end per ticket"]),
        _row(["Latency, p95", ms(m["latency_ms"]["p95"]), f"Model call p95 {ms(m['model_latency_ms']['p95'])}"]),
        _row([
            "Tokens per ticket", f"{m['tokens']['input_mean'] or 0:.0f} in, {m['tokens']['output_mean'] or 0:.0f} out",
            f"{m['tokens']['billed_tickets']} tickets billed",
        ]),
        _row([
            "Cost per ticket", usd(m["cost_usd"]["per_ticket"]),
            f"{usd(m['cost_usd']['per_1000_tickets'], 2)} per 1,000 tickets, run total {usd(m['cost_usd']['total'])}",
        ]),
        _row(["Fully correct tickets", f"{m['fully_correct']} of {m['tickets']}", "Priority, category, team and injection all right"]),
        "",
        "## Priority confusion matrix",
        "",
        "Rows are the expected priority and columns are the final priority after the business rules.",
        "",
        _row(["Expected", *priority["columns"], "Recall"]),
        _row(["---"] * (len(priority["columns"]) + 2)),
    ]
    for label, counts in zip(priority["labels"], priority["confusion"], strict=True):
        cells = [f"**{c}**" if i == priority["labels"].index(label) else str(c) for i, c in enumerate(counts)]
        lines.append(_row([f"**{label}**", *cells, pct(priority["per_class"][label]["recall"])]))
    lines += [
        "",
        "## Injection",
        "",
        f"Missed attempts: {_ids(injection['missed'])}. False positives: {_ids(injection['false_positive_ids'])}.",
        "",
        "## Business rules that changed a decision",
        "",
    ]
    rules = m["rules_fired"]
    lines += [f"- `{rule}` fired on {tickets(count)}" for rule, count in rules.items()] or ["- None"]
    lines += ["", f"## Failures ({len(report['failures'])})", ""]
    if report["failures"]:
        lines += [_row(["Ticket", "Subject", "Wrong on", "Expected", "Got"]), _row(["---"] * 5)]
        for item in report["failures"]:
            wrong = item["mismatches"]
            lines.append(
                _row([
                    item["id"],
                    item["subject"].replace("|", "/"),
                    ", ".join(wrong),
                    ", ".join(_label(f, item["expected"]) for f in wrong),
                    ", ".join(_label(f, item["actual"]) for f in wrong),
                ])
            )
    else:
        lines.append("Every ticket was triaged as labelled.")
    return "\n".join(lines) + "\n"
