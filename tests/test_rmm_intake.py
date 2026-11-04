"""RMM intake: de-duplication, resets, idempotency, settings and the SQL in rmm.store."""

from types import SimpleNamespace
from typing import Any

import pytest
from fakes_integrations import (
    IntegrationFakeStore,
    install_integration_store,
    load_rmm_fixture,
)
from psycopg import sql

from triageops import alerts
from triageops.config import settings
from triageops.rmm import config as rmm_config
from triageops.rmm import intake
from triageops.rmm import store as rmm_store
from triageops.rmm.config import RmmSettings, rmm_settings, signing_secret
from triageops.rmm.models import RmmAlert
from triageops.rmm.parsers import parse_alert


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> IntegrationFakeStore:
    monkeypatch.setattr(rmm_settings, "dedupe_minutes", 30)
    return install_integration_store(monkeypatch)


def ninja(activity_id: int = 1, **overrides: Any) -> RmmAlert:
    return parse_alert({**load_rmm_fixture("ninjaone_triggered"), "id": activity_id, **overrides})


def handle(alert: RmmAlert, **kwargs: Any) -> intake.RmmOutcome:
    return intake.handle_alert(None, alert, client_hash="client-hash", **kwargs)


def occurrence_events(fake: IntegrationFakeStore, ticket_id: Any) -> list[str]:
    return [e["type"] for e in fake.list_events(None, ticket_id) if e["type"] in ("received", "rmm_repeat")]


# New alerts -----------------------------------------------------------------------

def test_a_new_alert_is_triaged_into_a_ticket(fake: IntegrationFakeStore) -> None:
    outcome = handle(ninja())
    assert (outcome.action, outcome.occurrences, outcome.priority_hint) == ("created", 1, "P2")
    ticket = outcome.ticket
    assert ticket["channel"] == "rmm"
    assert ticket["status"] == "triaged"
    assert ticket["customer"] == "Acme Ltd"
    assert ticket["subject"] == "Disk Free Space < 10% on ACME-FS01"
    (received,) = fake.events_of(ticket["id"], "received")
    assert received["detail"] == {
        "rmm_key": "ninjaone:1042:b7e4d1c0-2f9a-4e6b-8d35-91a0c7f2e614",
        "vendor": "ninjaone",
        "device": "ACME-FS01",
        "condition": "Disk Free Space < 10%",
        "severity": "major",
        "occurrences": 1,
        "channel": "rmm",
        "source_id": "rmm:ninjaone:1:TRIGGERED",
        "priority_hint": "P2",
    }


# De-duplication -------------------------------------------------------------------

def test_repeats_within_the_window_collapse_and_count(fake: IntegrationFakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_alert", lambda ticket, reason: sent.append(ticket["ref"]) or alerts.AlertResult("skipped"))
    first = handle(ninja(1, severity="CRITICAL"))
    assert len(sent) == 1, "a new P1 alert ticket alerts once"

    fake.advance(minutes=10)
    second = handle(ninja(2, severity="CRITICAL"))
    fake.advance(minutes=10)
    third = handle(ninja(3, severity="CRITICAL"))

    assert (second.action, second.occurrences) == ("collapsed", 2)
    assert (third.action, third.occurrences) == ("collapsed", 3)
    assert second.ticket["id"] == third.ticket["id"] == first.ticket["id"]
    assert len(fake.tickets) == 1
    assert fake.calls.count("insert_ticket") == 1 and fake.calls.count("update_ticket") == 1, "no new triage"
    assert len(sent) == 1, "a repeat sends no new alert"
    repeats = fake.events_of(first.ticket["id"], "rmm_repeat")
    assert [r["detail"]["description"] for r in repeats] == [
        "Repeat alert collapsed into this ticket, occurrence 2",
        "Repeat alert collapsed into this ticket, occurrence 3",
    ]
    detail = repeats[0]["detail"]
    assert detail == {
        "description": "Repeat alert collapsed into this ticket, occurrence 2",
        "rmm_key": "ninjaone:1042:b7e4d1c0-2f9a-4e6b-8d35-91a0c7f2e614",
        "source_id": "rmm:ninjaone:2:TRIGGERED",
        "occurrences": 2,
        "severity": "critical",
        "occurred_at": "2026-11-04T15:12:17.418226+00:00",
    }
    assert all("free space is 6.2%" not in str(r["detail"]) for r in repeats), "no raw alert message in repeats"


def test_the_window_rolls_from_the_last_occurrence(fake: IntegrationFakeStore) -> None:
    first = handle(ninja(1))
    for activity_id in (2, 3, 4):
        fake.advance(minutes=25)
        outcome = handle(ninja(activity_id))
        assert outcome.action == "collapsed"
        assert outcome.ticket["id"] == first.ticket["id"]
    assert outcome.occurrences == 4, "75 minutes after the first alert, still one ticket"

    fake.advance(minutes=31)
    fresh = handle(ninja(5))
    assert (fresh.action, fresh.occurrences) == ("created", 1)
    assert fresh.ticket["id"] != first.ticket["id"]
    assert len(fake.tickets) == 2


def test_the_window_is_strict(fake: IntegrationFakeStore) -> None:
    handle(ninja(1))
    fake.advance(minutes=30)
    assert handle(ninja(2)).action == "created"


@pytest.mark.parametrize(
    "change",
    [{"deviceId": 2077}, {"sourceConfigUid": "a1f0e5d2-0000-4000-8000-000000000001"}],
    ids=["other device", "other condition"],
)
def test_a_different_device_or_condition_is_a_new_ticket(fake: IntegrationFakeStore, change: dict[str, Any]) -> None:
    first = handle(ninja(1))
    fake.advance(minutes=2)
    other = handle(ninja(2, **change))
    assert other.action == "created"
    assert other.ticket["id"] != first.ticket["id"]


@pytest.mark.parametrize("status", ["resolved", "failed"])
def test_a_closed_ticket_is_not_reused(fake: IntegrationFakeStore, status: str) -> None:
    first = handle(ninja(1))
    fake.set_status(first.ticket["id"], status)
    fake.advance(minutes=1)
    again = handle(ninja(2))
    assert (again.action, again.occurrences) == ("created", 1)
    assert again.ticket["id"] != first.ticket["id"]


def test_the_window_can_be_configured(fake: IntegrationFakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    handle(ninja(1))
    fake.advance(minutes=6)
    assert handle(ninja(2), dedupe_minutes=5).action == "created"
    monkeypatch.setattr(rmm_settings, "dedupe_minutes", 60)
    fake.advance(minutes=45)
    assert handle(ninja(3)).action == "collapsed"


# Idempotency ----------------------------------------------------------------------

def test_a_retried_delivery_is_a_duplicate_and_not_counted(fake: IntegrationFakeStore) -> None:
    first = handle(ninja(1))
    retried = handle(ninja(1))
    assert (retried.action, retried.occurrences) == ("duplicate", 1)
    assert retried.ticket["id"] == first.ticket["id"]

    fake.advance(minutes=3)
    assert handle(ninja(2)).occurrences == 2
    retried_repeat = handle(ninja(2))
    assert (retried_repeat.action, retried_repeat.occurrences) == ("duplicate", 2)
    assert occurrence_events(fake, first.ticket["id"]) == ["received", "rmm_repeat"]
    assert handle(ninja(3)).occurrences == 3


def test_a_retry_after_the_window_is_still_a_duplicate(fake: IntegrationFakeStore) -> None:
    first = handle(ninja(1))
    fake.advance(hours=5)
    retried = handle(ninja(1))
    assert retried.action == "duplicate"
    assert retried.ticket["id"] == first.ticket["id"]


def test_a_race_caught_by_the_pipeline_is_reported_as_a_duplicate(
    fake: IntegrationFakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle(ninja(1))
    fake.set_status(next(iter(fake.tickets)), "resolved")
    monkeypatch.setattr(rmm_store, "find_alert_delivery", lambda conn, source_id: None)
    outcome = handle(ninja(1))
    assert (outcome.action, outcome.occurrences) == ("duplicate", 1)
    assert len(fake.tickets) == 1


def test_an_alert_without_an_id_is_never_a_duplicate(fake: IntegrationFakeStore) -> None:
    alert = ninja(1, id=None)
    assert alert.source_id is None
    handle(alert)
    assert handle(alert).action == "collapsed"


# Resets ---------------------------------------------------------------------------

def test_a_reset_is_recorded_on_the_open_ticket(fake: IntegrationFakeStore) -> None:
    first = handle(ninja(1))
    fake.advance(minutes=40)
    reset = handle(parse_alert(load_rmm_fixture("ninjaone_reset")))
    assert (reset.action, reset.occurrences, reset.ticket["id"]) == ("reset", 1, first.ticket["id"])
    assert fake.tickets[str(first.ticket["id"])]["status"] == "triaged", "a person decides when to resolve"
    (event,) = fake.events_of(first.ticket["id"], "rmm_reset")
    assert event["detail"] == {
        "description": "The RMM reported that the alert condition has cleared",
        "rmm_key": "ninjaone:1042:b7e4d1c0-2f9a-4e6b-8d35-91a0c7f2e614",
        "source_id": "rmm:ninjaone:48219902:RESET",
        "severity": "major",
    }

    retried = handle(parse_alert(load_rmm_fixture("ninjaone_reset")))
    assert retried.action == "duplicate"
    assert len(fake.events_of(first.ticket["id"], "rmm_reset")) == 1


def test_a_reset_with_no_open_ticket_is_ignored(fake: IntegrationFakeStore) -> None:
    outcome = handle(parse_alert(load_rmm_fixture("ninjaone_reset")))
    assert (outcome.action, outcome.ticket, outcome.occurrences) == ("ignored", None, 0)
    assert outcome.reason == "No open ticket matches this cleared alert"
    assert fake.tickets == {} and fake.events == []


def test_a_reset_looks_back_seven_days_and_skips_resolved_tickets(fake: IntegrationFakeStore) -> None:
    first = handle(ninja(1))
    fake.advance(days=6)
    assert handle(ninja(2, statusCode="RESET")).action == "reset"
    fake.advance(days=2)
    assert handle(ninja(3, statusCode="RESET")).action == "ignored"

    second = handle(ninja(4))
    fake.set_status(second.ticket["id"], "resolved")
    assert handle(ninja(5, statusCode="RESET")).action == "ignored"
    assert first.ticket["id"] != second.ticket["id"]


def test_a_flapping_alert_stays_on_one_ticket(fake: IntegrationFakeStore) -> None:
    first = handle(ninja(1))
    fake.advance(minutes=5)
    assert handle(ninja(2, statusCode="RESET")).action == "reset"
    fake.advance(minutes=5)
    again = handle(ninja(3))
    assert (again.action, again.occurrences, again.ticket["id"]) == ("collapsed", 2, first.ticket["id"])


def test_datto_raised_then_resolved(fake: IntegrationFakeStore) -> None:
    raised = handle(parse_alert(load_rmm_fixture("datto_raised")))
    assert (raised.action, raised.priority_hint) == ("created", "P1")
    assert raised.ticket["customer"] == "Acme Ltd - Head Office"
    fake.advance(minutes=12)
    resolved = handle(parse_alert(load_rmm_fixture("datto_resolved")))
    assert (resolved.action, resolved.ticket["id"]) == ("reset", raised.ticket["id"])


# Settings -------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "minutes"), [(None, 30), ("45", 45), ("0", 1), ("5000", 1440), ("soon", 30)])
def test_dedupe_minutes_from_the_environment(monkeypatch: pytest.MonkeyPatch, raw: str | None, minutes: int) -> None:
    if raw is None:
        monkeypatch.delenv("RMM_DEDUP_MINUTES", raising=False)
    else:
        monkeypatch.setenv("RMM_DEDUP_MINUTES", raw)
    monkeypatch.setenv("RMM_WEBHOOK_SECRET", "  rmm-secret  ")
    loaded = RmmSettings.from_env()
    assert (loaded.dedupe_minutes, loaded.webhook_secret) == (minutes, "rmm-secret")


def test_signing_secret_prefers_the_rmm_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rmm_settings, "webhook_secret", "")
    assert signing_secret() == settings.webhook_secret
    monkeypatch.setattr(rmm_settings, "webhook_secret", "rmm-only")
    assert signing_secret() == "rmm-only"
    assert rmm_config.clamp_minutes(-3) == 1 and rmm_config.clamp_minutes(90) == 90


# SQL --------------------------------------------------------------------------------

class RecordingConn:
    """Records ``execute`` calls and answers every one with ``row``."""

    def __init__(self, row: Any) -> None:
        self.row = row
        self.calls: list[tuple[Any, Any]] = []

    def execute(self, query: Any, params: Any = None) -> Any:
        self.calls.append((query, params))
        return SimpleNamespace(fetchone=lambda: self.row)


OPEN = ["new", "triaged", "escalated"]
T_FIELDS = '"t"."id", "t"."ref", "t"."created_at"'


def composed(query: sql.Composable) -> str:
    return query.as_string(None)


def test_find_alert_delivery_sql() -> None:
    conn = RecordingConn({"id": "t1"})
    assert rmm_store.find_alert_delivery(conn, "rmm:ninjaone:1:TRIGGERED") == {"id": "t1"}
    ((query, params),) = conn.calls
    assert params == ("rmm:ninjaone:1:TRIGGERED",)
    text = composed(query)
    assert text.startswith(f"select {T_FIELDS}")
    assert '"t"."is_demo" from tickets t where t.id = (' in text
    assert "e.type in ('received', 'rmm_repeat') and e.detail->>'source_id' = %s order by e.id limit 1)" in text
    assert "client_hash" not in text
    assert text.count("%s") == len(params)


def test_find_recent_alert_ticket_sql() -> None:
    conn = RecordingConn(None)
    assert rmm_store.find_recent_alert_ticket(conn, "ninjaone:1:c", 30) is None
    ((query, params),) = conn.calls
    assert params == ("ninjaone:1:c", 30, OPEN)
    text = composed(query)
    assert text.startswith(f"select {T_FIELDS}")
    assert "from ticket_events e join tickets t on t.id = e.ticket_id" in text
    assert "e.type in ('received', 'rmm_repeat') and e.detail->>'rmm_key' = %s" in text
    assert "e.created_at > now() - make_interval(mins => %s::int)" in text
    assert "t.status = any(%s)" in text
    assert text.endswith("order by e.created_at desc, e.id desc limit 1")
    assert text.count("%s") == len(params)


def test_count_alert_occurrences_sql() -> None:
    conn = RecordingConn({"n": 3})
    assert rmm_store.count_alert_occurrences(conn, "ticket-uuid", "ninjaone:1:c") == 3
    ((query, params),) = conn.calls
    assert params == ("ticket-uuid", "ninjaone:1:c")
    assert composed(query) == (
        "select count(*) as n from ticket_events e where e.ticket_id = %s "
        "and e.type in ('received', 'rmm_repeat') and e.detail->>'rmm_key' = %s"
    )


def test_find_open_alert_ticket_sql() -> None:
    conn = RecordingConn({"id": "t2"})
    assert rmm_store.find_open_alert_ticket(conn, "datto:d:c") == {"id": "t2"}
    ((query, params),) = conn.calls
    assert params == ("datto:d:c", 7, OPEN)
    text = composed(query)
    assert text.startswith(f"select {T_FIELDS}")
    assert "e.created_at > now() - make_interval(days => %s::int)" in text
    assert "t.status = any(%s)" in text
    assert text.count("%s") == len(params)
