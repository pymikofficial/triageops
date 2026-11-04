"""The evaluation harness: scoring on fixed fake results, the dataset, the offline pipeline and the CLI."""

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from fakes import VALID_TRIAGE, tool_response

from evals import harness, report, run_eval, scoring
from triageops import rag, store, triage
from triageops.config import settings
from triageops.psa import sync as psa_sync
from triageops.psa.config import psa_settings
from triageops.rules import find_critical_security_trigger
from triageops.schemas import TicketIn
from triageops.taxonomy import CATEGORIES, CHANNELS, PRIORITIES, TEAMS

# Fixed fake results --------------------------------------------------------------


def result(
    id: str,
    expected: tuple[str, str, str, bool],
    actual: tuple[str, str, str, bool] | None,
    **extra: Any,
) -> dict[str, Any]:
    keys = ("priority", "category", "team", "injection")
    record = {
        "id": id,
        "subject": f"Subject {id}",
        "channel": "email",
        "tags": [],
        "expected": dict(zip(keys, expected, strict=True)),
        "actual": dict(zip(keys, actual, strict=True)) if actual else None,
        "rules_fired": [],
        "model": "claude-haiku-5-5",
        "fallback": False,
        "latency_ms": 1000,
        "pipeline_ms": 1100,
        "input_tokens": 1000,
        "output_tokens": 200,
    }
    record.update(extra)
    return record


RANSOM = ("P1", "Security", "Security Ops", False)
OUTAGE = ("P1", "Network", "Infrastructure L2", False)
MFA = ("P2", "Access & Identity", "Service Desk L1", False)
PRINTER = ("P3", "Hardware", "Service Desk L1", False)
HOWTO = ("P4", "Email & M365", "Cloud & M365", False)
INJECTED = ("P3", "Hardware", "Service Desk L1", True)

FIXED = [
    result("T1", RANSOM, RANSOM, pipeline_ms=900, rules_fired=["critical_security_p1"]),
    result("T2", OUTAGE, ("P2", "Network", "Infrastructure L2", False), pipeline_ms=1200),
    result("T3", OUTAGE, None, error="RuntimeError", pipeline_ms=50, input_tokens=None, output_tokens=None),
    result("T4", MFA, ("P1", "Access & Identity", "Service Desk L1", False), pipeline_ms=1500),
    result("T5", PRINTER, PRINTER, pipeline_ms=1000),
    result("T6", HOWTO, ("P4", "Email & M365", "Service Desk L1", True), pipeline_ms=800),
    result("T7", INJECTED, INJECTED, pipeline_ms=2000, rules_fired=["injection_markers"]),
    result("T8", ("P3", "Software", "Service Desk L1", True), ("P4", "Software", "Service Desk L1", False),
           pipeline_ms=5, latency_ms=2, fallback=True, fallback_reason="Claude did not answer in time",
           input_tokens=None, output_tokens=None),  # fmt: skip
]


@pytest.fixture
def scored() -> dict[str, Any]:
    return scoring.score(FIXED, model="claude-haiku-5-5")


def test_percentile_uses_nearest_rank() -> None:
    assert scoring.percentile(list(range(1, 21)), 95) == 19
    assert scoring.percentile([4, 1, 3, 2], 50) == 2
    assert scoring.percentile([7], 95) == 7
    assert scoring.percentile([], 95) is None


def test_ratio_is_none_without_a_denominator() -> None:
    assert scoring.ratio(2, 3) == 0.6667
    assert scoring.ratio(0, 0) is None


def test_p1_recall_counts_errors_as_misses_and_fails_the_gate(scored: dict[str, Any]) -> None:
    p1 = scored["metrics"]["p1"]
    assert (p1["expected"], p1["caught"], p1["recall"]) == (3, 1, 0.3333)
    assert p1["missed"] == ["T2", "T3"]
    assert p1["false_alarms"] == ["T4"]
    assert p1["precision"] == 0.5
    assert scored["gate"] == {"metric": "p1_recall", "target": 1.0, "value": 0.3333, "passed": False}


def test_gate_passes_only_at_full_p1_recall() -> None:
    perfect = scoring.score([result("A", RANSOM, RANSOM), result("B", PRINTER, ("P1", *PRINTER[1:]))])
    assert perfect["gate"]["passed"] is True
    assert perfect["metrics"]["p1"]["precision"] == 0.5
    no_p1 = scoring.score([result("C", PRINTER, PRINTER)])
    assert no_p1["gate"]["value"] is None and no_p1["gate"]["passed"] is False


def test_priority_confusion_matrix(scored: dict[str, Any]) -> None:
    priority = scored["metrics"]["priority"]
    assert priority["columns"] == ["P1", "P2", "P3", "P4", "none"]
    assert priority["confusion"] == [
        [1, 1, 0, 0, 1],  # T1 right, T2 under triaged, T3 crashed
        [1, 0, 0, 0, 0],  # T4 over triaged
        [0, 0, 2, 1, 0],  # T5 and T7 right, T8 under triaged
        [0, 0, 0, 1, 0],  # T6 right
    ]
    assert (priority["correct"], priority["total"], priority["accuracy"]) == (4, 8, 0.5)
    assert (priority["under_triaged"], priority["over_triaged"], priority["unscored"]) == (2, 1, 1)
    assert priority["per_class"]["P3"] == {"support": 3, "recall": 0.6667, "precision": 1.0}
    assert priority["per_class"]["P2"]["precision"] == 0.0


def test_category_and_team_accuracy(scored: dict[str, Any]) -> None:
    metrics = scored["metrics"]
    assert (metrics["category"]["correct"], metrics["category"]["accuracy"]) == (7, 0.875)
    assert (metrics["team"]["correct"], metrics["team"]["accuracy"]) == (6, 0.75)
    assert metrics["team"]["per_label"]["Cloud & M365"] == {"support": 1, "correct": 0}


def test_injection_detection_and_false_positives(scored: dict[str, Any]) -> None:
    injection = scored["metrics"]["injection"]
    assert (injection["expected"], injection["detected"], injection["detection_rate"]) == (2, 1, 0.5)
    assert injection["missed"] == ["T8"]
    assert (injection["false_positives"], injection["negatives"]) == (1, 6)
    assert injection["false_positive_ids"] == ["T6"]
    assert injection["false_positive_rate"] == 0.1667


def test_latency_excludes_crashes_and_model_latency_excludes_fallbacks(scored: dict[str, Any]) -> None:
    latency, model = scored["metrics"]["latency_ms"], scored["metrics"]["model_latency_ms"]
    assert latency["count"] == 7
    assert latency["mean"] == round((900 + 1200 + 1500 + 1000 + 800 + 2000 + 5) / 7, 1)
    assert latency["p95"] == 2000 and latency["p50"] == 1000 and latency["max"] == 2000
    assert model["count"] == 6 and model["mean"] == 1000


def test_tokens_and_cost_use_the_price_table(scored: dict[str, Any]) -> None:
    tokens, cost = scored["metrics"]["tokens"], scored["metrics"]["cost_usd"]
    assert tokens == {"billed_tickets": 6, "input_total": 6000, "output_total": 1200,
                      "input_mean": 1000.0, "output_mean": 200.0}  # fmt: skip
    # Haiku 5.5 is $0.10 in and $0.50 out per million tokens.
    assert cost["total"] == pytest.approx(0.0012)
    assert cost["per_ticket"] == pytest.approx(0.0002)
    assert cost["per_1000_tickets"] == pytest.approx(0.2)


def test_unknown_model_has_no_cost() -> None:
    unpriced = scoring.score([result("A", RANSOM, RANSOM, model="claude-unknown")], model="claude-unknown")
    assert unpriced["metrics"]["cost_usd"]["per_ticket"] is None
    assert unpriced["metrics"]["tokens"]["input_total"] == 1000


def test_fallbacks_errors_and_rules_are_counted(scored: dict[str, Any]) -> None:
    metrics = scored["metrics"]
    assert metrics["fallbacks"] == {"count": 1, "ids": ["T8"], "reasons": {"Claude did not answer in time": 1}}
    assert metrics["errors"] == {"count": 1, "ids": ["T3"]}
    assert metrics["rules_fired"] == {"critical_security_p1": 1, "injection_markers": 1}
    assert metrics["fully_correct"] == 3  # T1, T5 and T7


def test_failures_list_missed_p1s_first(scored: dict[str, Any]) -> None:
    failures = scored["failures"]
    assert [f["id"] for f in failures] == ["T2", "T3", "T4", "T8", "T6"]
    crashed = failures[1]
    assert crashed["mismatches"] == ["priority", "category", "team", "injection"]
    assert crashed["actual"] is None and crashed["error"] == "RuntimeError"
    assert failures[-1]["mismatches"] == ["team", "injection"]


# Markdown report -----------------------------------------------------------------


def fixed_report(mode: str = "claude") -> dict[str, Any]:
    return {
        "run": {
            "generated_at": "2026-11-04T12:00:00Z", "mode": mode, "model": "claude-haiku-5-5",
            "prompt_sha256": "abc123", "dataset": {"path": "evals/dataset.jsonl", "tickets": 60},
        },
        **scoring.score(FIXED, model="claude-haiku-5-5"),
    }  # fmt: skip


def test_markdown_report_has_gate_matrix_and_failures() -> None:
    text = report.render(fixed_report())
    assert "## Release gate: FAILED" in text
    assert "P1 recall is 33.3% against a target of 100.0%" in text
    assert "| **P1** | **1** | 1 | 0 | 0 | 1 | 33.3% |" in text
    assert "| T2 | Subject T2 | priority | P1 | P2 |" in text
    crashed_row = (
        "| T3 | Subject T3 | priority, category, team, injection | P1, Network, Infrastructure L2, not flagged | "
        "no result, no result, no result, no result |"
    )
    assert crashed_row in text
    assert "> 1 ticket fell back to keyword rules because the Claude call failed (T8)" in text
    assert "`critical_security_p1` fired on 1 ticket\n" in text


def test_markdown_report_flags_a_fallback_only_run() -> None:
    text = report.render(fixed_report(mode="rules-fallback"))
    assert "These numbers come from the keyword fallback, not from Claude" in text


def test_markdown_report_follows_the_house_style() -> None:
    text = report.render(fixed_report()) + report.render(fixed_report(mode="rules-fallback"))
    assert "—" not in text
    assert ";" not in text
    assert not re.search(r"\b\w+n't\b|\b(?:it|that|there|we|you|they)'(?:s|re|ll|ve|d)\b", text, re.IGNORECASE)


# Dataset -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dataset() -> list[harness.Case]:
    return harness.load_dataset()


def test_dataset_covers_the_brief(dataset: list[harness.Case]) -> None:
    assert len(dataset) == 60
    assert {c.category for c in dataset} == set(CATEGORIES)
    assert {c.priority for c in dataset} == set(PRIORITIES)
    assert {c.team for c in dataset} == set(TEAMS)
    # RMM alerts are machine generated, arrive through /api/webhooks/rmm and have their own
    # fixtures under tests/fixtures/rmm, so the dataset covers the channels people write in.
    assert {c.channel for c in dataset} == set(CHANNELS) - {"rmm"}
    p1 = [c for c in dataset if c.priority == "P1"]
    assert len(p1) >= 12
    for kind in ("ransomware", "breach", "outage", "payment-fraud"):
        assert any(f"p1:{kind}" in c.tags for c in p1), kind
    injections = [c for c in dataset if c.injection]
    assert len(injections) >= 8
    assert len({tag for c in injections for tag in c.tags if tag.startswith("injection:")}) >= 8, "varied styles"
    for tricky in ("tricky:angry-low-impact", "tricky:calm-critical", "tricky:mixed"):
        assert sum(tricky in c.tags for c in dataset) >= 2, tricky


def test_dataset_tickets_are_valid_api_input(dataset: list[harness.Case]) -> None:
    for case in dataset:
        TicketIn(subject=case.subject, body=case.body, customer=case.customer)
        assert case.rationale, case.id


def test_dataset_labels_agree_with_the_business_rules(dataset: list[harness.Case]) -> None:
    for case in dataset:
        if find_critical_security_trigger(f"{case.subject}\n{case.body}"):
            assert (case.priority, case.team) == ("P1", "Security Ops"), case.id
        if case.category == "Security":
            assert case.priority in ("P1", "P2"), case.id


def test_dataset_text_follows_the_house_style(dataset: list[harness.Case]) -> None:
    for case in dataset:
        text = f"{case.subject} {case.body} {case.rationale}"
        assert "—" not in text and ";" not in text, case.id
        assert not re.search(r"\b\w+n't\b|\b(?:I|it|that|we|you|they)'(?:m|s|re|ll|ve|d)\b", text), case.id


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"expected_priority": "P5"}, "not in the taxonomy"),
        ({"expected_team": "Helpdesk"}, "not in the taxonomy"),
        ({"injection": "yes"}, "true or false"),
        ({"subject": ""}, "subject is missing"),
    ],
)
def test_bad_dataset_rows_are_rejected(change: dict[str, Any], message: str) -> None:
    row = {"id": "X", "channel": "email", "subject": "Printer", "body": "It is offline today.",
           "expected_category": "Hardware", "expected_priority": "P3", "expected_team": "Service Desk L1",
           "injection": False, **change}  # fmt: skip
    with pytest.raises(harness.DatasetError, match=message):
        harness.parse_case(row)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    line = json.dumps({"id": "X", "channel": "email", "subject": "Printer", "body": "It is offline today.",
                       "expected_category": "Hardware", "expected_priority": "P3",
                       "expected_team": "Service Desk L1", "injection": False})  # fmt: skip
    path = tmp_path / "dupes.jsonl"
    path.write_text(f"{line}\n{line}\n")
    with pytest.raises(harness.DatasetError, match="duplicate"):
        harness.load_dataset(path)


# Offline pipeline ----------------------------------------------------------------


def test_lexical_retriever_finds_the_matching_runbook() -> None:
    retriever = harness.LexicalRetriever()
    matches = retriever.search(None, "Files encrypted with a ransom note on the file server")
    assert matches[0].slug == "ransomware-first-response"
    assert len(matches) == 3 and all(0 < m.similarity <= 1 for m in matches)
    assert retriever.search(None, "VPN error 809 from home")[0].slug == "vpn-error-809"
    assert retriever.search(None, "zzz qqq") == []


def test_eval_store_is_safe_across_threads() -> None:
    eval_store = harness.EvalStore()

    def work() -> None:
        ticket = eval_store.insert_ticket(None, channel="email", subject="s", body="b")
        eval_store.add_events(None, ticket["id"], [("received", {})])

    threads = [threading.Thread(target=work) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({t["ref"] for t in eval_store.tickets.values()}) == 20
    assert all(len(events) == 1 for events in eval_store.events.values())


def test_offline_pipeline_restores_everything() -> None:
    before = (store.insert_ticket, rag.search, settings.triage_model)
    settings.alert_webhook_url = "https://hooks.example.com/alert"
    with harness.offline_pipeline(harness.EvalStore(), harness.LexicalRetriever(), model="claude-test"):
        assert settings.triage_model == "claude-test" and settings.alert_webhook_url == ""
        assert store.insert_ticket is not before[0] and rag.search is not before[1]
    assert (store.insert_ticket, rag.search, settings.triage_model) == before
    assert settings.alert_webhook_url == "https://hooks.example.com/alert"


def test_offline_pipeline_never_creates_psa_tickets(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with a production environment loaded must not send the dataset to the PSA."""
    monkeypatch.setattr(psa_settings, "provider", "halopsa")
    calls: list[str] = []
    monkeypatch.setattr(psa_sync.factory, "get_adapter", lambda *a, **k: calls.append("adapter") or None)
    with harness.offline_pipeline(harness.EvalStore(), harness.LexicalRetriever()):
        assert psa_settings.provider == ""
        assert psa_sync.sync_after_triage(object(), {"ref": "TKT-1104-0001"}) is None
    assert psa_settings.provider == "halopsa"
    assert calls == []


class OracleAnthropic:
    """A thread safe fake Claude that answers each ticket from a table keyed by subject."""

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.models: list[str] = []
        self.messages = type("Messages", (), {"create": self._create})()

    def _create(self, **kwargs: Any) -> Any:
        self.models.append(kwargs["model"])
        content = kwargs["messages"][0]["content"]
        subject = re.search(r"<subject>(.*?)</subject>", content).group(1)
        subject = subject.replace("&lt;", "<").replace("&gt;", ">")
        return tool_response({**VALID_TRIAGE, **self.answers[subject]})


def oracle_for(cases: list[harness.Case]) -> OracleAnthropic:
    return OracleAnthropic(
        {
            c.subject: {"category": c.category, "priority": c.priority, "team": c.team, "injection_suspected": c.injection}
            for c in cases
        }
    )


@pytest.fixture
def no_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the eval must not call the embeddings service")

    monkeypatch.setattr(rag, "embed", refuse)


def test_run_cases_uses_the_real_rules_after_claude(
    dataset: list[harness.Case], monkeypatch: pytest.MonkeyPatch, no_embeddings: None
) -> None:
    ransomware = next(c for c in dataset if c.id == "EV-001")
    printer = next(c for c in dataset if "Printer shows offline" in c.subject)
    fake = OracleAnthropic({
        ransomware.subject: {"category": "Security", "priority": "P3", "team": "Service Desk L1"},
        printer.subject: {"category": "Hardware", "priority": "P3", "team": "Service Desk L1"},
    })  # fmt: skip
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(triage, "_client", lambda key: fake)

    first, second = harness.run_cases([ransomware, printer], model="claude-haiku-5-5", concurrency=4)

    assert fake.models == ["claude-haiku-5-5", "claude-haiku-5-5"]
    assert first["model_proposed"] == {"category": "Security", "priority": "P3", "team": "Service Desk L1"}
    assert first["actual"]["priority"] == "P1" and first["actual"]["team"] == "Security Ops"
    assert first["actual"]["status"] == "escalated"
    assert first["rules_fired"] == ["critical_security_p1"]
    assert (first["input_tokens"], first["output_tokens"], first["fallback"]) == (1200, 310, False)
    assert second["id"] == printer.id and scoring.mismatches(second) == []
    assert first["pipeline_ms"] is not None and first["latency_ms"] is not None


def test_run_case_records_a_crash(dataset: list[harness.Case], monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(conn: Any, inbound: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(triage, "process_ticket", crash)
    record = harness.run_case(dataset[0])
    assert record["actual"] is None and record["error"] == "RuntimeError"


# CLI -----------------------------------------------------------------------------


def test_cli_refuses_to_run_without_a_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_eval.main(["--out-dir", str(tmp_path)]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_cli_offline_run_writes_json_and_markdown(tmp_path: Path, no_embeddings: None) -> None:
    code = run_eval.main(["--offline", "--limit", "5", "--out-dir", str(tmp_path), "--name", "smoke", "--no-gate"])
    assert code == 0
    data = json.loads((tmp_path / "smoke.json").read_text())
    assert data["schema_version"] == scoring.SCHEMA_VERSION
    assert data["run"]["mode"] == "rules-fallback" and data["run"]["model"] == "rules-fallback"
    assert data["run"]["limit"] == 5 and data["run"]["dataset"]["tickets"] == 60
    assert len(data["results"]) == 5 and data["metrics"]["tickets"] == 5
    assert data["metrics"]["fallbacks"]["count"] == 5
    assert data["metrics"]["cost_usd"]["per_ticket"] is None
    assert (tmp_path / "smoke.md").read_text().startswith("# TriageOps evaluation results")
    assert settings.anthropic_api_key == "", "the key must be restored after an offline run"


def test_cli_with_fake_claude_scores_the_whole_dataset(
    tmp_path: Path, dataset: list[harness.Case], monkeypatch: pytest.MonkeyPatch, no_embeddings: None
) -> None:
    fake = oracle_for(dataset)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(triage, "_client", lambda key: fake)

    code = run_eval.main(["--model", "claude-haiku-5-5", "--out-dir", str(tmp_path)])

    data = json.loads((tmp_path / "latest.json").read_text())
    metrics = data["metrics"]
    assert code == 0 and data["gate"]["passed"] is True
    assert len(fake.models) == 60 and set(fake.models) == {"claude-haiku-5-5"}
    assert metrics["priority"]["accuracy"] == 1.0
    assert metrics["category"]["accuracy"] == 1.0 and metrics["team"]["accuracy"] == 1.0
    assert metrics["injection"]["detection_rate"] == 1.0
    # A perfect model still gets one false positive, from the keyword check in the business rules.
    decoy = next(c.id for c in dataset if "tricky:injection-false-positive" in c.tags)
    assert metrics["injection"]["false_positive_ids"] == [decoy]
    assert metrics["fallbacks"]["count"] == 0
    assert metrics["cost_usd"]["per_ticket"] == pytest.approx((1200 * 0.10 + 310 * 0.50) / 1_000_000)
    assert data["run"]["prompt_sha256"] == run_eval.prompt_digest()


def test_cli_exits_1_when_the_gate_fails(
    tmp_path: Path, dataset: list[harness.Case], monkeypatch: pytest.MonkeyPatch, no_embeddings: None
) -> None:
    fake = oracle_for(dataset)
    calm = next(c for c in dataset if "tricky:calm-critical" in c.tags and c.category == "Network")
    fake.answers[calm.subject] = {**fake.answers[calm.subject], "priority": "P3"}
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(triage, "_client", lambda key: fake)

    assert run_eval.main(["--out-dir", str(tmp_path)]) == 1
    data = json.loads((tmp_path / "latest.json").read_text())
    assert data["metrics"]["p1"]["missed"] == [calm.id]
    assert data["failures"][0]["id"] == calm.id


def test_committed_results_match_the_dataset() -> None:
    """The /evals page renders this file, so it must be a complete run of the current dataset."""
    data = json.loads((harness.EVALS_DIR / "results" / "latest.json").read_text())
    assert data["schema_version"] == scoring.SCHEMA_VERSION
    assert data["run"]["dataset"]["sha256"] == harness.dataset_digest()
    assert data["metrics"]["tickets"] == data["run"]["dataset"]["tickets"] == 60
    for key in ("gate", "metrics", "failures", "results"):
        assert key in data
