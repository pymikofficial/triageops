"""HaloPSA adapter against the mock server: token, request body, answer shapes, retries and lookups."""

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from mock_psa import (
    HALO_CLIENT_ID,
    HALO_CLIENT_SECRET,
    TICKET_ROW,
    FakeClock,
    MockPsaServer,
    client_for,
    halo_settings,
)

from triageops.psa.base import PsaError, PsaTicket
from triageops.psa.halopsa import HaloPsaAdapter, parse_created_id


def ticket(**overrides: Any) -> PsaTicket:
    return PsaTicket.from_ticket({**TICKET_ROW, **overrides})


def make(
    server: MockPsaServer | None = None, **env: str
) -> tuple[HaloPsaAdapter, MockPsaServer, FakeClock]:
    server = server or MockPsaServer()
    clock = FakeClock()
    adapter = HaloPsaAdapter(halo_settings(**env), client=client_for(server), sleep=clock.sleep, clock=clock)
    return adapter, server, clock


def form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


# Token -------------------------------------------------------------------------------

def test_token_is_a_client_credentials_form_post() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket())
    (token_request,) = server.calls("halo_token")
    assert str(token_request.url) == "https://acme.halopsa.com/auth/token"
    assert token_request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert form(token_request) == {
        "grant_type": "client_credentials", "client_id": HALO_CLIENT_ID, "client_secret": HALO_CLIENT_SECRET,
        "scope": "all",
    }  # fmt: skip
    create = server.calls("halo_create")[0]
    assert create.headers["authorization"] == "Bearer halo-token-1"


def test_tenant_is_sent_as_a_query_parameter_only_when_set() -> None:
    adapter, server, _ = make(MockPsaServer(halo_tenant="acme"), HALOPSA_TENANT="acme")
    adapter.create_ticket(ticket())
    assert server.calls("halo_token")[0].url.params["tenant"] == "acme"

    adapter, server, _ = make()
    adapter.create_ticket(ticket())
    assert "tenant" not in server.calls("halo_token")[0].url.params


def test_custom_auth_url_and_scope_are_used() -> None:
    adapter, server, _ = make(HALOPSA_AUTH_URL="https://login.acme.example/auth", HALOPSA_SCOPE="edit:tickets")
    adapter.create_ticket(ticket())
    request = server.calls("halo_token")[0]
    assert str(request.url) == "https://login.acme.example/auth/token"
    assert form(request)["scope"] == "edit:tickets"


def test_token_is_cached_until_sixty_seconds_before_expiry() -> None:
    adapter, server, clock = make(MockPsaServer(token_expires_in=600))
    adapter.create_ticket(ticket())
    adapter.create_ticket(ticket(ref="TKT-1104-0002"))
    assert len(server.calls("halo_token")) == 1

    clock.advance(539)
    adapter.create_ticket(ticket(ref="TKT-1104-0003"))
    assert len(server.calls("halo_token")) == 1
    clock.advance(2)
    adapter.create_ticket(ticket(ref="TKT-1104-0004"))
    assert len(server.calls("halo_token")) == 2


def test_token_is_refreshed_once_on_a_401_and_the_create_is_resent() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket())
    server.expire_tokens()

    result = adapter.create_ticket(ticket(ref="TKT-1104-0002"))

    assert result.external_id == "54322"
    assert len(server.calls("halo_token")) == 2
    creates = server.calls("halo_create")
    assert [c.headers["authorization"] for c in creates] == [
        "Bearer halo-token-1", "Bearer halo-token-1", "Bearer halo-token-2",
    ]  # fmt: skip
    assert result.attempts == 2


def test_a_second_401_fails_without_another_refresh() -> None:
    adapter, server, _ = make()
    server.fail_next(401, times=5, route="halo_create", body={"error": "unauthorised"})
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert caught.value.status_code == 401
    assert "even after a refresh" in caught.value.message
    assert len(server.calls("halo_token")) == 2
    assert len(server.calls("halo_create")) == 2


def test_token_500_html_is_an_authentication_failure() -> None:
    adapter, server, _ = make(MockPsaServer(halo_tenant="acme"))  # no tenant configured, Halo answers HTML
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    error = caught.value
    assert error.status_code == 500 and not error.retryable
    assert error.message == (
        "HaloPSA authentication failed with HTTP 500. Check HALOPSA_CLIENT_ID, HALOPSA_CLIENT_SECRET and HALOPSA_TENANT"
    )
    assert "<html" not in error.message
    assert server.calls("halo_create") == []


def test_token_invalid_client_is_summarised_without_the_secret() -> None:
    adapter, server, _ = make(HALOPSA_CLIENT_SECRET="wrong-secret-value")
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    error = caught.value
    assert error.status_code == 401
    assert "invalid_client" not in error.message and "Invalid client credentials" in error.message
    assert "wrong-secret-value" not in error.message
    assert len(server.calls("halo_token")) == 1, "a 401 from the token endpoint is not retried"


def test_token_answer_without_an_access_token_fails() -> None:
    adapter, server, _ = make()
    server.fail_next(200, route="halo_token", body={"token_type": "Bearer"})
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert "held no access token" in caught.value.message


# Request body --------------------------------------------------------------------

def test_request_body_is_an_array_holding_one_documented_ticket() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket())
    create = server.calls("halo_create")[0]
    assert str(create.url) == "https://acme.halopsa.com/api/Tickets"
    body = server.body(create)
    assert isinstance(body, list) and len(body) == 1
    sent = body[0]
    details = sent.pop("details")
    assert sent == {
        "summary": "TKT-1104-0001 Disk space low on SRV-FS01",
        "tickettype_id": 31,
        "third_party_id_string": "TKT-1104-0001",
        "reportedby": "TriageOps",
        "priority_id": 2,
        "category_1": "Hardware",
        "team": "Infrastructure L2",
        "dateoccurred": "2026-11-04T09:00:00.000Z",
    }
    assert "TriageOps ticket TKT-1104-0001" in details
    assert "File server disk nearly full." in details and "- Extend the volume" in details
    assert "SLA due: 04 Nov 2026 13:00 UTC" in details
    assert "Free space on C: is 3 percent" in details
    assert "draft must never reach" not in details


def test_configured_ids_and_priority_map_are_sent() -> None:
    adapter, server, _ = make(
        HALOPSA_DEFAULT_CLIENT_ID="12", HALOPSA_SITE_ID="0", HALOPSA_TEAM_ID="3", HALOPSA_PRIORITY_IDS="P2:21"
    )
    adapter.create_ticket(ticket())
    sent = server.body(server.calls("halo_create")[0])[0]
    assert (sent["client_id"], sent["site_id"], sent["team_id"], sent["priority_id"]) == (12, 0, 3, 21)
    assert "team" not in sent, "a configured team id wins over the team name"


def test_long_summary_is_capped_and_unknown_priority_omitted() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket(subject="x" * 400, priority=None, category=None, team=None, created_at=None))
    sent = server.body(server.calls("halo_create")[0])[0]
    assert len(sent["summary"]) == 250
    for key in ("priority_id", "category_1", "team", "dateoccurred"):
        assert key not in sent


def test_injection_note_is_added_to_the_details() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket(injection_suspected=True))
    details = server.body(server.calls("halo_create")[0])[0]["details"]
    assert "prompt injection suspected" in details


# Answer shapes -------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["object", "list", "wrapper"])
def test_every_documented_answer_shape_gives_the_id(shape: str) -> None:
    adapter, server, _ = make(MockPsaServer(halo_shape=shape))  # type: ignore[arg-type]
    result = adapter.create_ticket(ticket())
    assert (result.provider, result.external_id, result.external_ref) == ("halopsa", "54321", None)
    assert result.url == "https://acme.halopsa.com/tickets?id=54321"
    assert result.attempts == 1


@pytest.mark.parametrize(
    ("data", "expected"),
    [({"id": 5}, "5"), ([{"id": "6"}], "6"), ({"Ticket": {"ID": 7}, "Success": False}, "7"), ({}, None),
     ([], None), ({"id": 0}, None), ({"id": "abc"}, None), (None, None), ("text", None)],
)  # fmt: skip
def test_parse_created_id(data: Any, expected: str | None) -> None:
    assert parse_created_id(data) == expected


def test_empty_answer_is_resolved_by_a_lookup_when_the_ticket_exists() -> None:
    adapter, server, _ = make(MockPsaServer(halo_shape="empty"))
    result = adapter.create_ticket(ticket())
    assert result.external_id == "54321"
    assert len(server.calls("halo_create")) == 1
    assert server.calls("halo_search")[0].url.params["third_party_id_string"] == "TKT-1104-0001"


def test_empty_answer_without_a_ticket_is_an_error_and_is_not_resent() -> None:
    adapter, server, _ = make()
    server.fail_next(200, route="halo_create", body={})
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert caught.value.ambiguous and "may have been created" in caught.value.message
    assert len(server.calls("halo_create")) == 1


# Retries and lookups --------------------------------------------------------------

def test_429_with_retry_after_is_retried() -> None:
    adapter, server, clock = make()
    server.fail_next(429, route="halo_create", retry_after="1")
    result = adapter.create_ticket(ticket())
    assert result.attempts == 2 and clock.sleeps == [1.0]
    assert server.calls("halo_search") == []


def test_500_after_the_ticket_was_created_is_found_by_lookup_not_resent() -> None:
    adapter, server, clock = make()
    server.fail_next(500, route="halo_create", commit=True)
    result = adapter.create_ticket(ticket())
    assert result.external_id == "54321" and result.attempts == 1
    assert len(server.calls("halo_create")) == 1
    assert len(server.halo_tickets) == 1
    assert clock.sleeps == []


def test_500_before_the_ticket_was_created_is_resent_after_the_lookup() -> None:
    adapter, server, clock = make()
    server.fail_next(500, route="halo_create")
    result = adapter.create_ticket(ticket())
    assert result.attempts == 2 and len(server.halo_tickets) == 1
    assert server.routes == ["halo_token", "halo_create", "halo_search", "halo_create"]
    assert clock.sleeps == [0.5]


def test_read_timeout_after_commit_is_found_by_lookup() -> None:
    adapter, server, _ = make()
    server.fail_next_transport(httpx.ReadTimeout, route="halo_create", commit=True)
    result = adapter.create_ticket(ticket())
    assert result.external_id == "54321" and len(server.halo_tickets) == 1


def test_failed_lookup_reports_that_the_ticket_may_exist() -> None:
    adapter, server, _ = make()
    server.fail_next(502, route="halo_create", commit=True)
    server.fail_next(500, times=10, route="halo_search")
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    error = caught.value
    assert error.status_code == 502 and error.ambiguous and not error.retryable
    assert "may already exist in HaloPSA" in error.message
    assert len(server.calls("halo_create")) == 1


def test_lookup_without_the_reference_field_is_not_trusted() -> None:
    adapter, server, _ = make(MockPsaServer(halo_search_has_ref_field=False))
    server.fail_next(504, route="halo_create", commit=True)
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert "may already exist" in caught.value.message


def test_lookup_ignores_tickets_with_another_reference() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket(ref="TKT-1104-0009"))
    assert adapter.find_ticket("TKT-1104-0001", adapter._budget()) is None
    assert adapter.find_ticket("TKT-1104-0009", adapter._budget()) == "54321"


@pytest.mark.parametrize(
    ("status", "body", "text"),
    [(400, {"[0].summary": "Summary is required"}, "HaloPSA rejected the ticket with HTTP 400: [0].summary: Summary is required"),
     (404, {"message": "Not found"}, "HaloPSA rejected the ticket with HTTP 404: Not found")],
)  # fmt: skip
def test_client_errors_are_not_retried(status: int, body: dict[str, str], text: str) -> None:
    adapter, server, clock = make()
    server.fail_next(status, route="halo_create", body=body)
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert caught.value.message == text and caught.value.status_code == status
    assert not caught.value.retryable and len(server.calls("halo_create")) == 1 and clock.sleeps == []


def test_403_names_the_permission_problem() -> None:
    adapter, server, _ = make()
    server.fail_next(403, route="halo_create", body={"message": "Forbidden"})
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert "Check the permissions of the Halo API application" in caught.value.message


def test_retryable_status_left_after_every_attempt_is_marked_retryable() -> None:
    adapter, server, _ = make()
    server.fail_next(503, times=3, route="halo_create")
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert (caught.value.status_code, caught.value.attempts, caught.value.retryable) == (503, 3, True)


def test_error_messages_never_hold_secrets_or_the_token() -> None:
    adapter, server, _ = make()
    server.fail_next(
        400, route="halo_create",
        body={"message": f"bad request for {HALO_CLIENT_SECRET} with token halo-token-1 {'x' * 500}"},
    )  # fmt: skip
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    message = caught.value.message
    assert HALO_CLIENT_SECRET not in message and "halo-token-1" not in message
    assert len(message) < 300
