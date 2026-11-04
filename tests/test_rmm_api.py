"""POST /api/webhooks/rmm end to end through FastAPI, with the database and Claude faked."""

import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import pytest
from fakes import WEBHOOK_SECRET
from fakes_integrations import (
    IntegrationFakeStore,
    install_integration_store,
    load_rmm_fixture,
    signed_rmm_headers,
)
from fastapi.testclient import TestClient

from triageops import db
from triageops.app import app
from triageops.config import settings
from triageops.db import DatabaseUnavailable
from triageops.rmm.config import rmm_settings

URL = "/api/webhooks/rmm"
RESPONSE_KEYS = {"action", "ticket", "occurrences", "priority_hint", "vendor"}
BAD_SIGNATURE = {"error": "Invalid or expired signature."}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> IntegrationFakeStore:
    monkeypatch.setattr(rmm_settings, "webhook_secret", "")
    monkeypatch.setattr(rmm_settings, "dedupe_minutes", 30)
    return install_integration_store(monkeypatch)


@pytest.fixture
def client(fake: IntegrationFakeStore) -> TestClient:
    return TestClient(app)


def encode(body: Any) -> bytes:
    return json.dumps(body).encode()


def post(client: TestClient, body: Any, *, source: str | None = None, secret: str = WEBHOOK_SECRET) -> Any:
    raw = body if isinstance(body, bytes) else encode(body)
    params = {"source": source} if source is not None else None
    return client.post(URL, content=raw, headers=signed_rmm_headers(raw, secret=secret), params=params)


# Actions --------------------------------------------------------------------------

def test_a_ninjaone_alert_creates_a_triaged_ticket(client: TestClient, fake: IntegrationFakeStore) -> None:
    response = post(client, load_rmm_fixture("ninjaone_triggered"))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert (body["action"], body["occurrences"], body["priority_hint"], body["vendor"]) == ("created", 1, "P2", "ninjaone")
    ticket = body["ticket"]
    assert ticket["ref"].startswith("TKT-")
    assert (ticket["channel"], ticket["status"], ticket["customer"]) == ("rmm", "triaged", "Acme Ltd")
    assert ticket["subject"] == "Disk Free Space < 10% on ACME-FS01"
    assert "client_hash" not in ticket
    assert isinstance(ticket["created_at"], str)
    assert response.headers["x-frame-options"] == "DENY"


def test_repeats_collapse_and_retries_are_duplicates(client: TestClient, fake: IntegrationFakeStore) -> None:
    first = load_rmm_fixture("ninjaone_triggered")
    created = post(client, first).json()

    fake.advance(minutes=12)
    repeat = post(client, {**first, "id": first["id"] + 1}).json()
    assert (repeat["action"], repeat["occurrences"]) == ("collapsed", 2)
    assert repeat["ticket"]["ref"] == created["ticket"]["ref"]

    retried = post(client, first).json()
    assert (retried["action"], retried["occurrences"]) == ("duplicate", 2)
    assert retried["ticket"]["ref"] == created["ticket"]["ref"]
    assert len(fake.tickets) == 1

    fake.advance(minutes=31)
    fresh = post(client, {**first, "id": first["id"] + 2}).json()
    assert (fresh["action"], fresh["occurrences"]) == ("created", 1)
    assert fresh["ticket"]["ref"] != created["ticket"]["ref"]


def test_a_reset_is_recorded_and_status_is_unchanged(client: TestClient, fake: IntegrationFakeStore) -> None:
    created = post(client, load_rmm_fixture("ninjaone_triggered")).json()
    reset = post(client, load_rmm_fixture("ninjaone_reset")).json()
    assert set(reset) == RESPONSE_KEYS
    assert (reset["action"], reset["occurrences"]) == ("reset", 1)
    assert reset["ticket"]["ref"] == created["ticket"]["ref"]
    assert reset["ticket"]["status"] == "triaged"

    detail = client.get(f"/api/tickets/{created['ticket']['ref']}").json()
    assert detail["events"][-1]["type"] == "rmm_reset"
    assert detail["events"][-1]["detail"]["description"] == "The RMM reported that the alert condition has cleared"


def test_a_reset_with_nothing_open_is_ignored(client: TestClient) -> None:
    body = post(client, load_rmm_fixture("ninjaone_reset")).json()
    assert body == {
        "action": "ignored",
        "ticket": None,
        "occurrences": 0,
        "priority_hint": "P2",
        "vendor": "ninjaone",
        "reason": "No open ticket matches this cleared alert",
    }


def test_an_acknowledgement_is_ignored_with_200(client: TestClient, fake: IntegrationFakeStore) -> None:
    response = post(client, load_rmm_fixture("ninjaone_acknowledged"))
    assert response.status_code == 200
    assert response.json() == {
        "action": "ignored",
        "ticket": None,
        "occurrences": 0,
        "priority_hint": None,
        "vendor": "ninjaone",
        "reason": "NinjaOne status ACKNOWLEDGED does not raise or clear an alert",
    }
    assert fake.tickets == {}


def test_datto_raised_and_resolved(client: TestClient) -> None:
    raised = post(client, load_rmm_fixture("datto_raised"), source="datto").json()
    assert (raised["action"], raised["vendor"], raised["priority_hint"]) == ("created", "datto", "P1")
    assert raised["ticket"]["customer"] == "Acme Ltd - Head Office"
    resolved = post(client, load_rmm_fixture("datto_resolved"), source="dattormm").json()
    assert (resolved["action"], resolved["ticket"]["ref"]) == ("reset", raised["ticket"]["ref"])


def test_datto_api_alert_object(client: TestClient) -> None:
    body = post(client, load_rmm_fixture("datto_api_alert")).json()
    assert (body["action"], body["vendor"]) == ("created", "datto")
    assert body["ticket"]["subject"] == "Device has gone offline on ACME-LT042"


def test_datto_without_a_priority_gets_no_hint(client: TestClient) -> None:
    body = post(client, load_rmm_fixture("datto_priority_unexpanded")).json()
    assert (body["action"], body["priority_hint"]) == ("created", None)


def test_a_form_encoded_datto_body(client: TestClient) -> None:
    raw = urlencode(
        {"alert_uid": "u-77", "device_uid": "d-77", "device_hostname": "ACME-PC07", "alert_type": "Ping",
         "alert_message": "Ping to gateway failed", "priority": "Moderate", "site_name": "Acme Ltd"}
    ).encode()  # fmt: skip
    headers = signed_rmm_headers(raw, content_type="application/x-www-form-urlencoded; charset=utf-8")
    body = client.post(URL, content=raw, headers=headers).json()
    assert (body["action"], body["vendor"], body["priority_hint"]) == ("created", "datto", "P3")
    assert body["ticket"]["subject"] == "Ping on ACME-PC07"


# Rejected payloads ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("body", "source", "message"),
    [
        ({"text": "hello"}, None, "The alert format was not recognised. Add ?source=ninjaone or ?source=datto to the URL."),
        ({"activityType": "CONDITION", "statusCode": "TRIGGERED"}, None, "The NinjaOne alert has no deviceId."),
        ({"alerts": [{"alertUid": "a"}]}, None, "Send one Datto RMM alert per request. The relay must split a list of alerts."),
        ([1, 2], None, "The request body must be a JSON object."),
        ({"device_uid": "d"}, "connectwise", "Unknown source. Use ninjaone or datto."),
    ],
)
def test_unusable_payloads_are_422(
    client: TestClient, fake: IntegrationFakeStore, body: Any, source: str | None, message: str
) -> None:
    response = post(client, body, source=source)
    assert response.status_code == 422
    assert response.json() == {"error": message}
    assert fake.tickets == {}


def test_a_datto_test_send_is_422(client: TestClient) -> None:
    response = post(client, load_rmm_fixture("datto_test_send_unexpanded"))
    assert response.status_code == 422
    assert "Unexpanded placeholders count as missing" in response.json()["error"]


def test_the_source_parameter_is_capped(client: TestClient) -> None:
    response = post(client, load_rmm_fixture("datto_raised"), source="d" * 21)
    assert response.status_code == 422
    assert response.json()["error"].startswith("Check the source field.")


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe", b"", b"[" * 60_000])
def test_invalid_json_is_400(client: TestClient, raw: bytes) -> None:
    response = post(client, raw)
    assert response.status_code == 400
    assert response.json() == {"error": "The request body must be valid JSON."}


def test_large_bodies_are_413(client: TestClient) -> None:
    raw = encode({"message": "x" * 70_000})
    declared = client.post(URL, content=raw, headers=signed_rmm_headers(raw))
    assert declared.status_code == 413

    chunked = client.post(URL, content=iter([raw[:40_000], raw[40_000:]]), headers=signed_rmm_headers(raw))
    assert chunked.status_code == 413
    assert chunked.json() == {"error": "The request body is too large."}


def test_a_database_outage_is_503_so_the_relay_retries(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable():
        raise DatabaseUnavailable("no database URL accepted a connection")

    monkeypatch.setattr(db, "connect", unavailable)
    response = post(client, load_rmm_fixture("ninjaone_triggered"))
    assert response.status_code == 503


# Signatures -----------------------------------------------------------------------

def tamper(headers: dict[str, str], raw: bytes) -> tuple[dict[str, str], bytes]:
    return headers, raw.replace(b"MAJOR", b"MINOR")


@pytest.mark.parametrize(
    "case",
    ["no headers", "no signature", "no timestamp", "tampered body", "wrong secret", "replayed", "future", "not a number"],
)
def test_bad_signatures_are_401(
    client: TestClient, fake: IntegrationFakeStore, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    raw = encode(load_rmm_fixture("ninjaone_triggered"))
    now = int(time.time())
    headers = signed_rmm_headers(raw)
    if case == "no headers":
        headers = {"Content-Type": "application/json"}
    elif case == "no signature":
        headers.pop("X-TriageOps-Signature")
    elif case == "no timestamp":
        headers.pop("X-TriageOps-Timestamp")
    elif case == "tampered body":
        headers, raw = tamper(headers, raw)
    elif case == "wrong secret":
        headers = signed_rmm_headers(raw, secret="guess")
    elif case == "replayed":
        headers = signed_rmm_headers(raw, timestamp=now - 301)
    elif case == "future":
        headers = signed_rmm_headers(raw, timestamp=now + 301)
    elif case == "not a number":
        headers["X-TriageOps-Timestamp"] = "yesterday"

    with caplog.at_level(logging.WARNING, logger="triageops.rmm.routes"):
        response = client.post(URL, content=raw, headers=headers)
    assert response.status_code == 401
    assert response.json() == BAD_SIGNATURE
    assert fake.tickets == {}
    assert any("signature rejected" in record.getMessage() and "reason=" in record.getMessage() for record in caplog.records)
    assert WEBHOOK_SECRET not in caplog.text


def test_the_signature_is_checked_before_the_body_is_parsed(client: TestClient) -> None:
    response = client.post(URL, content=b"{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 401


def test_the_signature_covers_the_exact_bytes(client: TestClient) -> None:
    raw = encode(load_rmm_fixture("ninjaone_triggered"))
    reformatted = json.dumps(json.loads(raw), indent=2).encode()
    assert client.post(URL, content=reformatted, headers=signed_rmm_headers(raw)).status_code == 401


def test_rmm_webhook_secret_is_preferred(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rmm_settings, "webhook_secret", "rmm-relay-secret")
    alert = load_rmm_fixture("ninjaone_triggered")
    assert post(client, alert).status_code == 401, "WEBHOOK_SECRET no longer signs RMM deliveries"
    assert post(client, alert, secret="rmm-relay-secret").json()["action"] == "created"


def test_webhook_secret_is_the_fallback(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert post(client, load_rmm_fixture("ninjaone_triggered")).json()["action"] == "created"
    monkeypatch.setattr(settings, "webhook_secret", "")
    raw = encode(load_rmm_fixture("ninjaone_console_minimal"))
    response = client.post(URL, content=raw, headers=signed_rmm_headers(raw, secret=""))
    assert response.status_code == 401, "with no secret configured every request is refused"
