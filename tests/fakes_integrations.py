"""In-memory stand-ins for the RMM intake and PSA sync tests. No network, no Postgres.

``IntegrationFakeStore`` extends ``FakeStore`` with a controllable clock and
memory versions of the ``triageops.rmm.store`` queries, with the same
semantics as the SQL. ``install_integration_store`` swaps it in, like the
``fake_store`` fixture in conftest. Test modules define their own fixture
that calls it.
"""

import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import WEBHOOK_SECRET, FakeStore

from triageops import db, rag, store
from triageops.rmm import store as rmm_store
from triageops.security import sign_timestamped
from triageops.taxonomy import OPEN_STATUSES

DEFAULT_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
RMM_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rmm"
_OCCURRENCE_TYPES = ("received", "rmm_repeat")

# The conftest fake_store fixture patches the same names.
STORE_FUNCTIONS = (
    "insert_ticket", "update_ticket", "mark_failed", "get_ticket", "find_ticket_by_source",
    "count_recent_tickets_from", "count_demo_tickets_today", "add_events", "list_events", "kb_count",
)  # fmt: skip
RMM_STORE_FUNCTIONS = (
    "find_alert_delivery", "find_recent_alert_ticket", "count_alert_occurrences", "find_open_alert_ticket",
)  # fmt: skip


class IntegrationFakeStore(FakeStore):
    """``FakeStore`` with a clock and the RMM queries. Move time with ``advance``."""

    def __init__(self, now: datetime = DEFAULT_NOW) -> None:
        super().__init__()
        self.now = now

    def advance(self, **delta: float) -> datetime:
        """Move the clock forward, for example ``advance(minutes=31)``."""
        self.now += timedelta(**delta)
        return self.now

    def set_status(self, ticket_id: Any, status: str) -> None:
        self.tickets[str(ticket_id)]["status"] = status

    # Tickets and events stamped with the clock
    def insert_ticket(self, conn: Any, **fields: Any) -> dict[str, Any]:
        ticket = super().insert_ticket(conn, **fields)
        stored = self.tickets[str(ticket["id"])]
        stored["created_at"] = stored["updated_at"] = self.now
        return dict(stored)

    def add_events(self, conn: Any, ticket_id: Any, events: list[tuple[str, dict[str, Any]]]) -> None:
        start = len(self.events)
        super().add_events(conn, ticket_id, events)
        for event in self.events[start:]:
            event["created_at"] = self.now

    # RMM queries
    def _occurrences(self, rmm_key: str, since: datetime | None = None) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if event["type"] in _OCCURRENCE_TYPES
            and event["detail"].get("rmm_key") == rmm_key
            and (since is None or event["created_at"] > since)
        ]

    def _newest_open_ticket(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in sorted(events, key=lambda e: (e["created_at"], e["id"]), reverse=True):
            ticket = self.tickets[str(event["ticket_id"])]
            if ticket["status"] in OPEN_STATUSES:
                return dict(ticket)
        return None

    def find_alert_delivery(self, conn: Any, source_id: str) -> dict[str, Any] | None:
        for event in self.events:
            if event["type"] in _OCCURRENCE_TYPES and event["detail"].get("source_id") == source_id:
                return dict(self.tickets[str(event["ticket_id"])])
        return None

    def find_recent_alert_ticket(self, conn: Any, rmm_key: str, within_minutes: int) -> dict[str, Any] | None:
        return self._newest_open_ticket(self._occurrences(rmm_key, self.now - timedelta(minutes=within_minutes)))

    def count_alert_occurrences(self, conn: Any, ticket_id: Any, rmm_key: str) -> int:
        return sum(1 for event in self._occurrences(rmm_key) if event["ticket_id"] == ticket_id)

    def find_open_alert_ticket(self, conn: Any, rmm_key: str) -> dict[str, Any] | None:
        since = self.now - timedelta(days=rmm_store.RESET_LOOKBACK_DAYS)
        return self._newest_open_ticket(self._occurrences(rmm_key, since))

    # Helpers for assertions
    def events_of(self, ticket_id: Any, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["ticket_id"] == ticket_id and event["type"] == kind]


def load_rmm_fixture(name: str) -> dict[str, Any]:
    """A sample vendor payload from ``tests/fixtures/rmm``, for example ``"ninjaone_triggered"``."""
    return json.loads((RMM_FIXTURES / f"{name}.json").read_text())


def signed_rmm_headers(
    raw: bytes, *, secret: str = WEBHOOK_SECRET, timestamp: int | None = None, content_type: str = "application/json"
) -> dict[str, str]:
    """Headers the n8n relay sends to ``/api/webhooks/rmm``."""
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    return {
        "Content-Type": content_type,
        "X-TriageOps-Timestamp": stamp,
        "X-TriageOps-Signature": sign_timestamped(secret, stamp, raw),
    }


def install_integration_store(monkeypatch: pytest.MonkeyPatch) -> IntegrationFakeStore:
    """Swap ``store`` and ``rmm.store`` for memory, make ``db.connect`` yield a dummy and stub retrieval."""
    fake = IntegrationFakeStore()
    for name in STORE_FUNCTIONS:
        monkeypatch.setattr(store, name, getattr(fake, name))
    for name in RMM_STORE_FUNCTIONS:
        monkeypatch.setattr(rmm_store, name, getattr(fake, name))

    @contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(rag, "search", lambda conn, text, count=3: [])
    return fake
