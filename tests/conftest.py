"""Shared fixtures. Every test runs without network access or a real database."""

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import (  # noqa: E402
    CRON_SECRET,
    MCP_TOKEN,
    WEBHOOK_SECRET,
    WHATSAPP_APP_SECRET,
    WHATSAPP_VERIFY_TOKEN,
    FakeStore,
)

from triageops import db, rag, store  # noqa: E402
from triageops.config import settings  # noqa: E402
from triageops.psa.config import psa_settings  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known secrets, no Claude key and no outbound URLs unless a test opts in."""
    for name, value in {
        "anthropic_api_key": "",
        "webhook_secret": WEBHOOK_SECRET,
        "mcp_token": MCP_TOKEN,
        "cron_secret": CRON_SECRET,
        "whatsapp_app_secret": WHATSAPP_APP_SECRET,
        "whatsapp_verify_token": WHATSAPP_VERIFY_TOKEN,
        "alert_webhook_url": "",
        "embed_secret": "",
        "ip_hash_salt": "test-salt",
        "database_urls": [],
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture(autouse=True)
def psa_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches a real PSA, even when the shell sets PSA_PROVIDER. PSA tests opt in."""
    monkeypatch.setattr(psa_settings, "provider", "")


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """Swap the SQL layer for memory, make db.connect yield a dummy, and stub retrieval."""
    fake = FakeStore()
    for name in (
        "insert_ticket", "update_ticket", "mark_failed", "get_ticket", "find_ticket_by_source",
        "count_recent_tickets_from", "count_demo_tickets_today", "add_events", "list_events", "kb_count",
    ):  # fmt: skip
        monkeypatch.setattr(store, name, getattr(fake, name))

    @contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(rag, "search", lambda conn, text, count=3: [])
    return fake
