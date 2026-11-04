"""Analyst console actions: approve the reply, override, resolve and reopen, each with its audit event.

Also checks that resolved tickets leave the open counts and the SLA sweep, by reading the SQL the
real store functions send, since the tests never touch a database.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from admin_fakes import RecordingConnection, auth, install, triaged_ticket
from fakes import FakeStore
from fastapi.testclient import TestClient

from triageops import admin_auth, store, triage
from triageops.app import app
from triageops.taxonomy import OPEN_STATUSES


@pytest.fixture(autouse=True)
def fresh_lockouts() -> None:
    admin_auth.failed_logins.reset()


@pytest.fixture
def client(fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch, fake_store)
    return TestClient(app)


@pytest.fixture
def ticket(fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return triaged_ticket(monkeypatch)


def post(client: TestClient, ref: str, action: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(f"/api/admin/tickets/{ref}/{action}", json=body, headers=auth())


def events_of(fake_store: FakeStore, ticket: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [e["detail"] for e in fake_store.events if e["ticket_id"] == ticket["id"] and e["type"] == kind]


# Approve the reply -----------------------------------------------------------------

def test_approving_the_draft_unchanged(client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]) -> None:
    response = post(client, ticket["ref"], "reply", {"text": ticket["suggested_reply"]})
    assert response.status_code == 200
    body = response.json()
    assert body["ticket"]["status"] == ticket["status"], "approving a reply does not change the status"
    assert body["events"][-1]["type"] == "reply_approved"
    assert events_of(fake_store, ticket, "reply_approved") == [
        {
            "text": ticket["suggested_reply"],
            "edited": False,
            "delivery": "Recorded only. Nothing was sent to the customer.",
            "by": "analyst",
        }
    ]


def test_approving_an_edited_reply_records_the_new_text_masked(
    client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]
) -> None:
    text = "Hello Sam,\n\nYour temporary password: Hunter2-Spring! Please change it.\n\nService Desk"
    assert post(client, ticket["ref"], "reply", {"text": f"  {text}  "}).status_code == 200
    [detail] = events_of(fake_store, ticket, "reply_approved")
    assert detail["edited"] is True
    assert "Hunter2-Spring!" not in detail["text"]
    assert "[REDACTED:password]" in detail["text"]
    assert fake_store.get_ticket(None, ticket["ref"])["suggested_reply"] == ticket["suggested_reply"], "the draft is kept"


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "   "}, {}, {"text": "x" * 6001}, {"text": "ok", "send": True}])
def test_reply_approval_validates_the_text(client: TestClient, ticket: dict[str, Any], body: dict[str, Any]) -> None:
    assert post(client, ticket["ref"], "reply", body).status_code == 422


def test_actions_on_an_unknown_ticket_are_404(client: TestClient) -> None:
    for action, body in (
        ("reply", {"text": "Hello"}),
        ("override", {"priority": "P1", "reason": "Outage"}),
        ("resolve", {"note": "Fixed"}),
        ("reopen", {}),
    ):
        response = post(client, "TKT-0000-9999", action, body)
        assert response.status_code == 404
        assert response.json() == {"error": "Ticket not found."}


# Override --------------------------------------------------------------------------

def test_priority_override_records_from_and_to_and_moves_the_sla(
    client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]
) -> None:
    assert ticket["priority"] == "P3"
    response = post(client, ticket["ref"], "override", {"priority": "P1", "reason": "Whole office cannot print payroll"})
    assert response.status_code == 200
    updated = response.json()["ticket"]
    due = ticket["created_at"] + timedelta(hours=1)
    assert updated["priority"] == "P1"
    assert updated["sla_due_at"] == due.isoformat()
    assert events_of(fake_store, ticket, "human_override") == [
        {
            "field": "priority",
            "from": "P3",
            "to": "P1",
            "reason": "Whole office cannot print payroll",
            "by": "analyst",
            "sla_due_at": due.isoformat(),
        }
    ]
    assert response.json()["events"][-1]["type"] == "human_override", "the timeline in the response is current"


def test_team_override(client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]) -> None:
    response = post(client, ticket["ref"], "override", {"team": "Infrastructure L2", "reason": "Print server fault"})
    assert response.status_code == 200
    assert response.json()["ticket"]["team"] == "Infrastructure L2"
    assert response.json()["ticket"]["sla_due_at"] == ticket["sla_due_at"].isoformat(), "a team change keeps the SLA"
    [detail] = events_of(fake_store, ticket, "human_override")
    assert (detail["field"], detail["from"], detail["to"]) == ("team", "Service Desk L1", "Infrastructure L2")
    assert "sla_due_at" not in detail


def test_changing_both_writes_one_event_per_field(client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]) -> None:
    body = {"priority": "P2", "team": "Security Ops", "reason": "Looks like a compromised device"}
    assert post(client, ticket["ref"], "override", body).status_code == 200
    details = events_of(fake_store, ticket, "human_override")
    assert [(d["field"], d["to"]) for d in details] == [("priority", "P2"), ("team", "Security Ops")]
    assert {d["reason"] for d in details} == {"Looks like a compromised device"}


def test_an_unchanged_field_is_not_recorded(client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]) -> None:
    body = {"priority": "P3", "team": "Cloud & M365", "reason": "Printer is a cloud print queue"}
    assert post(client, ticket["ref"], "override", body).status_code == 200
    assert [d["field"] for d in events_of(fake_store, ticket, "human_override")] == ["team"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"priority": "P1"}, "Check the reason field"),
        ({"priority": "P1", "reason": "   "}, "Check the reason field"),
        ({"priority": "P1", "reason": "no"}, "Check the reason field"),
        ({"priority": "P1", "reason": "x" * 501}, "Check the reason field"),
        ({"priority": "P9", "reason": "Outage"}, "Check the priority field"),
        ({"team": "Sales", "reason": "Wrong team"}, "Check the team field"),
        ({"reason": "Nothing chosen"}, "Choose a priority or team that differs from the current one."),
        ({"priority": "P3", "team": "Service Desk L1", "reason": "Same again"}, "Choose a priority or team"),
    ],
)
def test_override_needs_a_reason_and_a_real_change(
    client: TestClient, fake_store: FakeStore, ticket: dict[str, Any], body: dict[str, Any], message: str
) -> None:
    response = post(client, ticket["ref"], "override", body)
    assert response.status_code == 422
    assert response.json()["error"].startswith(message)
    assert events_of(fake_store, ticket, "human_override") == []
    assert fake_store.get_ticket(None, ticket["ref"])["priority"] == "P3"


def test_override_reason_is_masked(client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]) -> None:
    reason = "Customer card 4111 1111 1111 1111 was charged twice"
    assert post(client, ticket["ref"], "override", {"team": "Account Management", "reason": reason}).status_code == 200
    [detail] = events_of(fake_store, ticket, "human_override")
    assert "4111" not in detail["reason"]


def test_lowering_priority_clears_a_breach_only_when_the_new_deadline_is_ahead(
    client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]
) -> None:
    stored = fake_store.tickets[str(ticket["id"])]
    stored.update(priority="P1", sla_breached=True, created_at=datetime.now(UTC) - timedelta(hours=2))
    response = post(client, ticket["ref"], "override", {"priority": "P3", "reason": "Single user, has a workaround"})
    assert response.json()["ticket"]["sla_breached"] is False

    stored.update(priority="P1", sla_breached=True, created_at=datetime.now(UTC) - timedelta(days=5))
    response = post(client, ticket["ref"], "override", {"priority": "P3", "reason": "Single user, has a workaround"})
    assert response.json()["ticket"]["sla_breached"] is True


def test_override_on_a_ticket_without_a_priority(client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]) -> None:
    fake_store.tickets[str(ticket["id"])].update(priority=None, status="failed")
    assert post(client, ticket["ref"], "override", {"priority": "P2", "reason": "Triage failed, set by hand"}).status_code == 200
    [detail] = events_of(fake_store, ticket, "human_override")
    assert (detail["from"], detail["to"]) == (None, "P2")


# Resolve and reopen ----------------------------------------------------------------

def test_resolve_records_the_note_and_stops_the_sla(client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]) -> None:
    response = post(client, ticket["ref"], "resolve", {"note": "Reserved a fixed IP for the printer"})
    assert response.status_code == 200
    assert response.json()["ticket"]["status"] == "resolved"
    assert "resolved" not in OPEN_STATUSES, "the SLA sweep and the open counts only look at open statuses"
    assert events_of(fake_store, ticket, "resolved") == [
        {"note": "Reserved a fixed IP for the printer", "from": "triaged", "to": "resolved", "sla": "met", "by": "analyst"}
    ]


@pytest.mark.parametrize("body", [{}, {"note": ""}, {"note": "ok"}, {"note": "x" * 2001}])
def test_resolve_needs_a_note(client: TestClient, ticket: dict[str, Any], body: dict[str, Any]) -> None:
    response = post(client, ticket["ref"], "resolve", body)
    assert response.status_code == 422
    assert response.json()["error"].startswith("Check the note field")


def test_a_resolved_ticket_is_locked_until_reopened(client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]) -> None:
    assert post(client, ticket["ref"], "resolve", {"note": "Fixed on site"}).status_code == 200
    for action, body in (
        ("resolve", {"note": "Fixed again"}),
        ("reply", {"text": "Hello"}),
        ("override", {"priority": "P1", "reason": "Outage"}),
    ):
        response = post(client, ticket["ref"], action, body)
        assert response.status_code == 409
        assert response.json() == {"error": "This ticket is resolved. Reopen it first."}


def test_reopen_restores_the_status_before_resolution(
    client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]
) -> None:
    fake_store.tickets[str(ticket["id"])]["status"] = "escalated"
    post(client, ticket["ref"], "resolve", {"note": "Contained and cleaned"})
    response = post(client, ticket["ref"], "reopen", {"note": "Customer says it is back"})
    assert response.status_code == 200
    assert response.json()["ticket"]["status"] == "escalated"
    assert events_of(fake_store, ticket, "reopened") == [
        {"from": "resolved", "to": "escalated", "by": "analyst", "note": "Customer says it is back"}
    ]
    assert [e["type"] for e in response.json()["events"]][-2:] == ["resolved", "reopened"]


def test_reopen_works_without_a_body(client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]) -> None:
    post(client, ticket["ref"], "resolve", {"note": "Fixed on site"})
    response = client.post(f"/api/admin/tickets/{ticket['ref']}/reopen", headers=auth())
    assert response.status_code == 200
    assert response.json()["ticket"]["status"] == "triaged"
    assert events_of(fake_store, ticket, "reopened") == [{"from": "resolved", "to": "triaged", "by": "analyst"}]


def test_reopen_falls_back_to_triaged_without_a_resolved_event(
    client: TestClient, fake_store: FakeStore, ticket: dict[str, Any]
) -> None:
    fake_store.tickets[str(ticket["id"])]["status"] = "resolved"
    assert post(client, ticket["ref"], "reopen", {}).json()["ticket"]["status"] == "triaged"


def test_only_a_resolved_ticket_can_be_reopened(client: TestClient, ticket: dict[str, Any]) -> None:
    response = post(client, ticket["ref"], "reopen", {})
    assert response.status_code == 409
    assert response.json() == {"error": "Only a resolved ticket can be reopened."}


def test_the_full_story_is_on_the_public_timeline(client: TestClient, ticket: dict[str, Any]) -> None:
    post(client, ticket["ref"], "override", {"priority": "P2", "reason": "Payroll run depends on it"})
    post(client, ticket["ref"], "reply", {"text": "Hello Sam, we are on it."})
    post(client, ticket["ref"], "resolve", {"note": "Printer moved to a reserved address"})
    post(client, ticket["ref"], "reopen", {})
    timeline = [event["type"] for event in client.get(f"/api/tickets/{ticket['ref']}").json()["events"]]
    assert timeline[-4:] == ["human_override", "reply_approved", "resolved", "reopened"]
    assert "triaged" in timeline


# Resolved tickets leave the open counts and the SLA sweep --------------------------

def test_queue_stats_count_only_open_statuses_as_open() -> None:
    totals = {"total": 3, "open": 1, "open_p1": 0, "sla_breached": 0, "injection_flagged": 0, "last_24h": 3, "avg_latency_ms": 0}
    groups = [{"dimension": "status", "value": "resolved", "n": 2}, {"dimension": "status", "value": "triaged", "n": 1}]
    conn = RecordingConnection(totals, groups)

    stats = store.queue_stats(conn)

    sql, params = conn.statements[0]
    assert "count(*) filter (where status = any(%s)) as open" in sql
    assert "count(*) filter (where status = any(%s) and priority = 'P1') as open_p1" in sql
    assert params == (list(OPEN_STATUSES), list(OPEN_STATUSES))
    assert all("resolved" not in statuses for statuses in params)
    assert stats["open"] == 1
    assert stats["by_status"]["resolved"] == 2


def test_sla_sweep_skips_resolved_tickets(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = RecordingConnection({"n": 0}, [])

    assert triage.check_sla(conn) == {"checked": 0, "breached": 0}

    assert len(conn.statements) == 2
    for sql, params in conn.statements:
        assert "status = any(%s)" in sql
        assert params == (list(OPEN_STATUSES),)
        assert "resolved" not in params[0]
    assert "update tickets set sla_breached = true" in conn.statements[1][0]
