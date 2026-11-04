"""Fakes for the analyst console tests. They build on ``fakes.FakeStore``, with no network and no Postgres."""

from typing import Any

import pytest
from fakes import VALID_TRIAGE, FakeAnthropic, FakeStore, tool_response

from triageops import admin_store, triage
from triageops.config import settings
from triageops.schemas import InboundTicket

ADMIN_TOKEN = "test-admin-token-0123456789abcdef"


def auth(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeAdminStore:
    """In-memory ``admin_store.list_overrides`` that reads the events held by a ``FakeStore``."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def list_overrides(self, conn: Any, limit: int = admin_store.MAX_EXPORT_ROWS) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in self.store.events:
            if event["type"] != "human_override":
                continue
            ticket = self.store.tickets[str(event["ticket_id"])]
            triaged = next(
                (e["detail"] for e in self.store.events if e["ticket_id"] == event["ticket_id"] and e["type"] == "triaged"),
                {},
            )
            detail = event["detail"]
            rows.append(
                {
                    "ref": ticket["ref"],
                    "channel": ticket["channel"],
                    "ticket_created_at": ticket["created_at"],
                    "subject": ticket["subject"],
                    "body": ticket["body"],
                    "event_id": event["id"],
                    "overridden_at": event["created_at"],
                    "field": detail.get("field"),
                    "from_value": detail.get("from"),
                    "to_value": detail.get("to"),
                    "reason": detail.get("reason"),
                    "claude_model": triaged.get("model"),
                    "claude_category": triaged.get("category"),
                    "claude_priority": triaged.get("priority"),
                    "claude_team": triaged.get("team"),
                    "claude_confidence": None if triaged.get("confidence") is None else str(triaged["confidence"]),
                }
            )
        return rows[:limit]


def install(monkeypatch: pytest.MonkeyPatch, fake_store: FakeStore) -> FakeAdminStore:
    fake = FakeAdminStore(fake_store)
    monkeypatch.setattr(admin_store, "list_overrides", fake.list_overrides)
    monkeypatch.setattr(settings, "admin_token", ADMIN_TOKEN)
    return fake


def triaged_ticket(
    monkeypatch: pytest.MonkeyPatch,
    subject: str = "Front office printer offline",
    body: str = "The printer in the front office shows offline on every laptop since this morning.",
    **decision: Any,
) -> dict[str, Any]:
    """Run one ticket through the real pipeline with a fake Claude, so a genuine triaged event exists."""
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(triage, "_client", lambda key: FakeAnthropic(tool_response({**VALID_TRIAGE, **decision})))
    inbound = InboundTicket(channel="portal", subject=subject, body=body, customer="Riverside Cafe Group")
    bundle = triage.process_ticket(None, inbound)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    return bundle.ticket


class RecordingConnection:
    """Records SQL and parameters and returns canned rows, to check queries without a database."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.statements: list[tuple[str, Any]] = []

    def execute(self, query: Any, params: Any = None) -> "RecordingConnection":
        text = query if isinstance(query, str) else query.as_string(None)
        self.statements.append((" ".join(text.split()), params))
        return self

    def fetchone(self) -> Any:
        return self.results.pop(0)

    def fetchall(self) -> Any:
        return self.results.pop(0)
