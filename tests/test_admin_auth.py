"""Analyst console authentication: the bearer token, constant time comparison and the failed attempt lockout."""

import hmac

import pytest
from admin_fakes import ADMIN_TOKEN, auth, install, triaged_ticket
from fakes import FakeStore
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from triageops import admin, admin_auth, security
from triageops.admin_auth import (
    LOCKOUT_WINDOW_SECONDS,
    MAX_FAILED_ATTEMPTS,
    FailedAttemptLimiter,
)
from triageops.app import app
from triageops.config import settings

TRIAGE_EVENTS = {"received", "masked", "kb_matched", "triaged", "rule_applied", "escalated", "alert_sent", "alert_skipped", "human_review"}


@pytest.fixture(autouse=True)
def fresh_lockouts() -> None:
    admin_auth.failed_logins.reset()


@pytest.fixture
def client(fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch, fake_store)
    return TestClient(app)


def test_login_accepts_the_admin_token(client: TestClient) -> None:
    response = client.post("/api/admin/login", headers=auth())
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {ADMIN_TOKEN}"},
        {"Authorization": ADMIN_TOKEN},
        {"Authorization": "Bearer "},
        {"Authorization": f"Bearer {ADMIN_TOKEN}x"},
    ],
)
def test_login_rejects_missing_or_wrong_tokens(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/api/admin/login", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorised."}
    assert response.headers["www-authenticate"] == 'Bearer realm="admin"'


def test_an_unset_admin_token_disables_the_console(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", "")
    for headers in ({}, auth(""), auth("anything")):
        response = client.post("/api/admin/login", headers=headers)
        assert response.status_code == 503
        assert response.json() == {"error": "The analyst console is not configured."}


def test_the_token_is_compared_in_constant_time(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", spy)
    assert client.post("/api/admin/login", headers=auth()).status_code == 200
    assert client.post("/api/admin/login", headers=auth("nope")).status_code == 401
    assert calls == [(ADMIN_TOKEN.encode(), ADMIN_TOKEN.encode()), (b"nope", ADMIN_TOKEN.encode())]


def test_every_admin_route_needs_the_token(client: TestClient, fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    ref = triaged_ticket(monkeypatch)["ref"]
    routes = [route for route in admin.router.routes if isinstance(route, APIRoute)]
    assert len(routes) == 6
    for route in routes:
        path = route.path.replace("{ticket_key}", ref)
        for method in route.methods:
            admin_auth.failed_logins.reset()
            response = client.request(method, path, json={"reason": "x"})
            assert response.status_code == 401, f"{method} {path} must need the token"
    assert [e for e in fake_store.events if e["type"] not in TRIAGE_EVENTS] == [], "nothing may be written"


def test_auth_is_checked_before_the_body_is_validated(client: TestClient) -> None:
    response = client.post("/api/admin/tickets/TKT-1104-0001/override", json={"priority": "P9"})
    assert response.status_code == 401


def test_failed_attempts_lock_the_client_out(client: TestClient) -> None:
    for _ in range(MAX_FAILED_ATTEMPTS):
        assert client.post("/api/admin/login", headers=auth("guess")).status_code == 401
    locked = client.post("/api/admin/login", headers=auth())
    assert locked.status_code == 429, "the right token must not get through during a lockout"
    assert locked.json()["error"] == "Too many failed sign in attempts. Please wait before trying again."
    assert 0 < int(locked.headers["retry-after"]) <= LOCKOUT_WINDOW_SECONDS


def test_lockout_is_per_client(client: TestClient) -> None:
    attacker = {"X-Forwarded-For": "203.0.113.9"}
    for _ in range(MAX_FAILED_ATTEMPTS):
        client.post("/api/admin/login", headers={**attacker, **auth("guess")})
    assert client.post("/api/admin/login", headers={**attacker, **auth()}).status_code == 429
    analyst = {"X-Forwarded-For": "198.51.100.4"}
    assert client.post("/api/admin/login", headers={**analyst, **auth()}).status_code == 200


def test_successful_requests_do_not_count_towards_the_lockout(client: TestClient) -> None:
    for _ in range(MAX_FAILED_ATTEMPTS * 3):
        assert client.post("/api/admin/login", headers=auth()).status_code == 200
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        client.post("/api/admin/login", headers=auth("guess"))
    assert client.post("/api/admin/login", headers=auth()).status_code == 200


def test_limiter_releases_the_lockout_after_the_window() -> None:
    limiter = FailedAttemptLimiter(limit=3, window_seconds=60)
    for second in (0, 10, 20):
        assert limiter.retry_after("client", now=second) == 0
        limiter.record_failure("client", now=second)
    assert limiter.retry_after("client", now=21) == 39, "locked until the first failure leaves the window"
    assert limiter.retry_after("other", now=21) == 0
    assert limiter.retry_after("client", now=61) == 0
    limiter.record_failure("client", now=61)
    assert limiter.retry_after("client", now=62) == 8


def test_public_ticket_routes_stay_read_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ref = triaged_ticket(monkeypatch)["ref"]
    assert client.get(f"/api/tickets/{ref}").status_code == 200
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, f"/api/tickets/{ref}", headers=auth()).status_code == 405
        assert client.request(method, "/api/tickets", headers=auth()).status_code == 405
