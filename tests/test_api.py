"""REST routes end to end through FastAPI, with the database, Claude and the copilot faked."""

import hashlib
import hmac
import json
import time

import pytest
from fakes import (
    CRON_SECRET,
    WEBHOOK_SECRET,
    WHATSAPP_APP_SECRET,
    WHATSAPP_VERIFY_TOKEN,
    FakeStore,
)
from fastapi.testclient import TestClient

from triageops import agent, db, store
from triageops import app as app_module
from triageops.agent import AgentReply
from triageops.app import app
from triageops.config import settings
from triageops.db import DatabaseUnavailable
from triageops.samples import SAMPLES_BY_KEY
from triageops.security import SlidingWindowLimiter, sign_timestamped


@pytest.fixture
def client(fake_store: FakeStore) -> TestClient:
    return TestClient(app)


def signed_headers(raw: bytes, timestamp: int | None = None, secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    return {
        "Content-Type": "application/json",
        "X-TriageOps-Timestamp": stamp,
        "X-TriageOps-Signature": sign_timestamped(secret, stamp, raw),
    }


def test_health_reports_status_with_security_headers(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": "1.1.0", "db": True, "ai": False, "kb_articles": 12, "region": "local"}
    headers = response.headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in headers["permissions-policy"]
    assert "access-control-allow-origin" not in headers


def test_health_is_503_when_the_database_is_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable():
        raise DatabaseUnavailable("no database URL accepted a connection")

    monkeypatch.setattr(db, "connect", unavailable)
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["db"] is False


def test_unknown_routes_return_json_errors_with_headers(client: TestClient) -> None:
    response = client.get("/api/nothing-here")
    assert response.status_code == 404
    assert "error" in response.json()
    assert response.headers["x-frame-options"] == "DENY"


# Public demo portal ------------------------------------------------------------------

def demo_payload(**overrides: str) -> dict[str, str]:
    return {**SAMPLES_BY_KEY["printer"].payload(), "channel": "portal", **overrides}


def test_demo_ticket_is_triaged_synchronously(client: TestClient, fake_store: FakeStore) -> None:
    response = client.post("/api/demo/tickets", json=demo_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["ticket"]["ref"].startswith("TKT-")
    assert body["ticket"]["status"] == "triaged"
    assert body["ticket"]["is_demo"] is True
    assert "client_hash" not in body["ticket"]
    assert [event["type"] for event in body["events"]][0] == "received"
    assert isinstance(body["timing_ms"], int)


def test_demo_honeypot_rejects_bots(client: TestClient, fake_store: FakeStore) -> None:
    response = client.post("/api/demo/tickets", json=demo_payload(website="https://spam.example"))
    assert response.status_code == 400
    assert fake_store.tickets == {}


def test_demo_validation_errors_name_the_field(client: TestClient) -> None:
    response = client.post("/api/demo/tickets", json=demo_payload(subject="Hi"))
    assert response.status_code == 422
    body = response.json()
    assert body["error"].startswith("Check the subject field.")
    assert body["fields"][0]["field"] == "subject"


@pytest.mark.parametrize(("attribute", "message"), [("recent_from_client", "an hour"), ("demo_today", "daily")])
def test_demo_rate_limits(client: TestClient, fake_store: FakeStore, attribute: str, message: str) -> None:
    setattr(fake_store, attribute, 10_000)
    response = client.post("/api/demo/tickets", json=demo_payload())
    assert response.status_code == 429
    assert message in response.json()["error"]
    assert int(response.headers["retry-after"]) > 0


def test_oversized_bodies_are_refused(client: TestClient) -> None:
    response = client.post("/api/demo/tickets", content=b"x" * 70_000, headers={"Content-Type": "application/json"})
    assert response.status_code == 413


# Signed webhooks ----------------------------------------------------------------------

def test_inbound_webhook_accepts_a_signed_ticket(client: TestClient) -> None:
    raw = json.dumps(SAMPLES_BY_KEY["howto"].payload() | {"channel": "webhook"}).encode()
    response = client.post("/api/webhooks/inbound", content=raw, headers=signed_headers(raw))
    assert response.status_code == 200
    assert response.json()["ticket"]["channel"] == "webhook"
    assert response.json()["duplicate"] is False


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/json"},
        "stale",
        "wrong-secret",
    ],
)
def test_inbound_webhook_rejects_bad_signatures(client: TestClient, fake_store: FakeStore, headers) -> None:
    raw = json.dumps(SAMPLES_BY_KEY["howto"].payload()).encode()
    if headers == "stale":
        headers = signed_headers(raw, timestamp=int(time.time()) - 301)
    elif headers == "wrong-secret":
        headers = signed_headers(raw, secret="guess")
    response = client.post("/api/webhooks/inbound", content=raw, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "Invalid or expired signature."}
    assert fake_store.tickets == {}


def test_signature_covers_the_exact_raw_bytes(client: TestClient) -> None:
    raw = json.dumps(SAMPLES_BY_KEY["howto"].payload()).encode()
    headers = signed_headers(raw)
    reformatted = json.dumps(json.loads(raw), indent=2).encode()
    assert client.post("/api/webhooks/inbound", content=reformatted, headers=headers).status_code == 401


def test_email_webhook_maps_a_postmark_payload(client: TestClient) -> None:
    raw = json.dumps(
        {
            "From": "laura.hughes@penninelogistics.co.uk",
            "FromName": "Laura Hughes",
            "Subject": "VPN error 809 when working from home",
            "TextBody": "The VPN fails every time with error 809 since my new router arrived.",
            "MailboxHash": "Pennine Logistics",
            "MessageID": "b7bc2f4a-e38e-4336-af7d-e6c392c2f817",
            "Headers": [{"Name": "X-Spam-Status", "Value": "No"}],
        }
    ).encode()
    response = client.post("/api/webhooks/email", content=raw, headers=signed_headers(raw))
    ticket = response.json()["ticket"]
    assert (ticket["channel"], ticket["customer"]) == ("email", "Pennine Logistics")
    assert ticket["requester"] == "Laura H. (lau***@penn***)"


# WhatsApp ------------------------------------------------------------------------------

def whatsapp_payload(text: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "447700900218", "profile": {"name": "Sam Patel"}}],
                            "messages": [
                                {"from": "447700900218", "id": "wamid.ABC", "type": "text", "text": {"body": text}},
                                {"from": "447700900218", "id": "wamid.IMG", "type": "image", "image": {}},
                            ],
                        }
                    }
                ]
            }
        ],
    }


def test_whatsapp_verification_handshake(client: TestClient) -> None:
    ok = client.get(
        "/api/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": WHATSAPP_VERIFY_TOKEN, "hub.challenge": "1158201444"},
    )
    assert ok.status_code == 200 and ok.text == "1158201444"
    assert ok.headers["content-type"].startswith("text/plain")
    denied = client.get(
        "/api/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"}
    )
    assert denied.status_code == 403


def test_whatsapp_messages_become_tickets(client: TestClient, fake_store: FakeStore) -> None:
    text = SAMPLES_BY_KEY["printer"].body
    raw = json.dumps(whatsapp_payload(text)).encode()
    signature = "sha256=" + hmac.new(WHATSAPP_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    response = client.post("/api/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": signature})
    assert response.json() == {"received": 1}
    (ticket,) = fake_store.tickets.values()
    assert ticket["channel"] == "whatsapp"
    assert ticket["subject"] == text[:60]
    assert ticket["requester"] == "Sam P. (+44 7*** ***218)"

    retried = client.post("/api/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": signature})
    assert retried.json() == {"received": 0}
    assert len(fake_store.tickets) == 1


def test_whatsapp_rejects_a_bad_signature(client: TestClient) -> None:
    raw = json.dumps(whatsapp_payload("Hello")).encode()
    assert client.post("/api/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 401


# Queue, copilot and cron ------------------------------------------------------------

def test_ticket_list_ignores_empty_filters_and_rejects_unknown_ones(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    def list_tickets(conn, **filters):
        captured.update(filters)
        return []

    monkeypatch.setattr(store, "list_tickets", list_tickets)
    assert client.get("/api/tickets?status=&priority=&channel=&q=&limit=50").json() == {"tickets": []}
    assert captured == {"status": None, "priority": None, "channel": None, "query": None, "limit": 50}
    assert client.get("/api/tickets?priority=P9").status_code == 422


def test_ticket_detail_by_ref_and_not_found(client: TestClient) -> None:
    created = client.post("/api/demo/tickets", json=demo_payload()).json()["ticket"]
    detail = client.get(f"/api/tickets/{created['ref']}").json()
    assert detail["ticket"]["id"] == created["id"]
    assert {"id", "created_at", "type", "detail"} <= set(detail["events"][0])
    assert client.get("/api/tickets/TKT-0000-0000").status_code == 404


def test_chat_needs_an_api_key(client: TestClient) -> None:
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "How many P1s are open?"}]})
    assert response.status_code == 503


def test_chat_returns_the_agent_reply(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    reply = AgentReply(reply="One P1 is open, TKT-1104-0001.", model="claude-sonnet-5-5", latency_ms=1400,
                       tools_used=[{"name": "search_tickets", "input": {"priority": "P1"}}])  # fmt: skip
    monkeypatch.setattr(agent, "run_agent", lambda messages: reply)
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "How many P1s are open?"}]})
    assert response.json() == {
        "reply": "One P1 is open, TKT-1104-0001.",
        "tools_used": [{"name": "search_tickets", "input": {"priority": "P1"}}],
        "model": "claude-sonnet-5-5",
        "latency_ms": 1400,
    }


def test_chat_is_rate_limited_and_validated(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "chat_limiter", SlidingWindowLimiter(limit=1, window_seconds=3600))
    message = {"messages": [{"role": "user", "content": "Hello"}]}
    assert client.post("/api/chat", json=message).status_code == 503
    assert client.post("/api/chat", json=message).status_code == 429
    too_many = {"messages": [{"role": "user", "content": "x"}] * 13}
    assert client.post("/api/chat", json=too_many).status_code == 422
    ends_with_assistant = {"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]}
    assert client.post("/api/chat", json=ends_with_assistant).status_code == 422


def test_cron_requires_the_secret(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "mark_sla_breaches", lambda conn: (4, []))
    assert client.get("/api/cron/sla").status_code == 401
    assert client.get("/api/cron/sla", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/cron/sla", headers={"Authorization": f"Bearer {CRON_SECRET}"})
    assert response.json() == {"checked": 4, "breached": 0}
