"""The overrides export: every human override next to Claude's original decision, as CSV gold labels."""

import csv
import io
from datetime import UTC, datetime
from typing import Any

import pytest
from admin_fakes import RecordingConnection, auth, install, triaged_ticket
from fakes import FakeStore
from fastapi.testclient import TestClient

from triageops import admin_auth, admin_store
from triageops.admin import CSV_COLUMNS, overrides_csv
from triageops.app import app


@pytest.fixture(autouse=True)
def fresh_lockouts() -> None:
    admin_auth.failed_logins.reset()


@pytest.fixture
def client(fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch, fake_store)
    return TestClient(app)


def read_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def override(client: TestClient, ref: str, **body: Any) -> None:
    response = client.post(f"/api/admin/tickets/{ref}/override", json=body, headers=auth())
    assert response.status_code == 200


def test_export_needs_the_token(client: TestClient) -> None:
    assert client.get("/api/admin/overrides.csv").status_code == 401
    assert client.get("/api/admin/overrides.csv", headers=auth("wrong")).status_code == 401


def test_empty_export_has_only_the_header(client: TestClient) -> None:
    response = client.get("/api/admin/overrides.csv", headers=auth())
    assert response.status_code == 200
    assert response.text == ",".join(CSV_COLUMNS) + "\r\n"


def test_export_puts_each_override_next_to_claudes_decision(
    client: TestClient, fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    printer = triaged_ticket(monkeypatch)
    ransomware = triaged_ticket(
        monkeypatch,
        subject="Files renamed with .locked extension",
        body="Every file on the shared drive now ends in .locked and there is a ransom note on the desktop.",
        category="Security",
        priority="P2",
        team="Service Desk L1",
        confidence=0.71,
    )
    assert ransomware["priority"] == "P1", "the business rule raised Claude's P2"
    override(client, printer["ref"], priority="P2", team="Infrastructure L2", reason="Payroll printer, whole office blocked")
    override(client, ransomware["ref"], team="Infrastructure L2", reason="Restore from backup, not a live attack")
    untouched = triaged_ticket(monkeypatch, subject="Password reset", body="Please reset the password for the new starter.")

    response = client.get("/api/admin/overrides.csv", headers=auth())

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="triageops-overrides-{datetime.now(UTC):%Y%m%d}.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    rows = read_csv(response.text)
    assert list(rows[0]) == list(CSV_COLUMNS)
    assert [(r["ticket_ref"], r["field"]) for r in rows] == [
        (printer["ref"], "priority"),
        (printer["ref"], "team"),
        (ransomware["ref"], "team"),
    ]
    assert untouched["ref"] not in response.text

    priority, team, security = rows
    assert priority["claude_value"] == "P3"
    assert (priority["from_value"], priority["to_value"]) == ("P3", "P2")
    assert priority["reason"] == "Payroll printer, whole office blocked"
    assert priority["claude_model"] == "claude-haiku-5-5"
    assert (priority["claude_category"], priority["claude_priority"], priority["claude_team"]) == (
        "Hardware", "P3", "Service Desk L1",
    )  # fmt: skip
    assert priority["claude_confidence"] == "0.86"
    assert priority["subject"] == "Front office printer offline"
    assert priority["channel"] == "portal"
    assert team["claude_value"] == "Service Desk L1"
    assert (team["from_value"], team["to_value"]) == ("Service Desk L1", "Infrastructure L2")

    # Claude said P2 and Service Desk L1. The rules moved it to Security Ops before the analyst stepped in.
    assert security["claude_value"] == "Service Desk L1"
    assert security["claude_priority"] == "P2"
    assert security["from_value"] == "Security Ops"
    assert security["to_value"] == "Infrastructure L2"


def test_cells_that_look_like_formulas_are_defused() -> None:
    row = {
        "ref": "TKT-1104-0001",
        "channel": "portal",
        "ticket_created_at": datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        "overridden_at": datetime(2026, 11, 4, 9, 30, tzinfo=UTC),
        "subject": '=HYPERLINK("http://evil.example","click")',
        "body": "+44 20 7946 0000\n@SUM(A1)\n-1",
        "field": "priority",
        "from_value": None,
        "to_value": "P1",
        "reason": "-Outage, quoted, with \"marks\"",
        "claude_priority": "P2",
    }
    [parsed] = read_csv(overrides_csv([row]))
    assert parsed["subject"] == "'=HYPERLINK(\"http://evil.example\",\"click\")"
    assert parsed["body"] == "'+44 20 7946 0000\n@SUM(A1)\n-1"
    assert parsed["reason"] == "'-Outage, quoted, with \"marks\""
    assert parsed["from_value"] == ""
    assert parsed["claude_value"] == "P2"
    assert parsed["ticket_created_at"] == "2026-11-04T09:00:00+00:00"
    assert parsed["claude_team"] == ""


def test_list_overrides_sql_joins_claudes_first_decision() -> None:
    conn = RecordingConnection([])
    assert admin_store.list_overrides(conn) == []
    [(sql, params)] = conn.statements
    assert "where o.type = 'human_override'" in sql
    assert "e.type = 'triaged' order by e.id limit 1" in sql
    assert "left join lateral" in sql, "an override on a ticket Claude never triaged is still exported"
    assert "client_hash" not in sql
    assert params == (admin_store.MAX_EXPORT_ROWS,)
