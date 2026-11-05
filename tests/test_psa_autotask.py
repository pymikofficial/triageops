"""Autotask adapter against the mock server: headers, zone discovery, body, itemId, errors and lookups."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mock_psa import (
    AUTOTASK_INTEGRATION_CODE,
    AUTOTASK_SECRET,
    AUTOTASK_USERNAME,
    TICKET_ROW,
    FakeClock,
    MockPsaServer,
    autotask_settings,
    client_for,
)

from triageops.psa.autotask import AutotaskAdapter, normalise_base_url, parse_item_id
from triageops.psa.base import PsaError, PsaTicket

NOW = datetime(2026, 11, 4, 9, 30, tzinfo=UTC)
BASE = "https://webservices15.autotask.net/atservicesrest/V1.0"


def ticket(**overrides: Any) -> PsaTicket:
    return PsaTicket.from_ticket({**TICKET_ROW, **overrides})


def make(server: MockPsaServer | None = None, **env: str) -> tuple[AutotaskAdapter, MockPsaServer, FakeClock]:
    server = server or MockPsaServer()
    clock = FakeClock()
    adapter = AutotaskAdapter(
        autotask_settings(**env), client=client_for(server), sleep=clock.sleep, clock=clock, now=lambda: NOW
    )
    return adapter, server, clock


# Headers and zone ------------------------------------------------------------------

def test_every_call_sends_the_api_user_headers() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket())
    create = server.calls("autotask_create")[0]
    assert create.headers["ApiIntegrationCode"] == AUTOTASK_INTEGRATION_CODE
    assert create.headers["UserName"] == AUTOTASK_USERNAME
    assert create.headers["Secret"] == AUTOTASK_SECRET
    assert create.headers["Content-Type"] == "application/json"
    assert str(create.url) == f"{BASE}/Tickets"


def test_zone_discovery_sends_the_user_and_no_credentials() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket())
    (zone,) = server.calls("autotask_zone")
    assert zone.url.host == "webservices.autotask.net"
    assert zone.url.path == "/atservicesrest/v1.0/zoneInformation"
    assert zone.url.params["user"] == AUTOTASK_USERNAME
    for header in ("Secret", "UserName", "ApiIntegrationCode"):
        assert header not in zone.headers
    assert adapter.web_url == "https://ww15.autotask.net/"


def test_zone_is_cached_across_creates() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket())
    adapter.create_ticket(ticket(ref="TKT-1104-0002"))
    assert len(server.calls("autotask_zone")) == 1
    assert len(server.calls("autotask_create")) == 2


def test_configured_base_url_skips_zone_discovery() -> None:
    adapter, server, _ = make(AUTOTASK_BASE_URL="https://webservices15.autotask.net/ATServicesRest/V1.0")
    adapter.create_ticket(ticket())
    assert server.calls("autotask_zone") == []
    assert str(server.calls("autotask_create")[0].url) == "https://webservices15.autotask.net/ATServicesRest/V1.0/Tickets"


@pytest.mark.parametrize(
    ("url", "expected"),
    [("https://webservices3.autotask.net/atservicesrest/", "https://webservices3.autotask.net/atservicesrest/V1.0"),
     ("https://webservices3.autotask.net/ATServicesRest", "https://webservices3.autotask.net/ATServicesRest/V1.0"),
     ("https://webservices3.autotask.net/atservicesrest/v1.0/", "https://webservices3.autotask.net/atservicesrest/v1.0")],
)  # fmt: skip
def test_base_url_is_pinned_to_v1(url: str, expected: str) -> None:
    assert normalise_base_url(url) == expected


def test_unknown_user_zone_error_is_permanent() -> None:
    adapter, server, _ = make(MockPsaServer(autotask_known_users=()))
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    error = caught.value
    assert error.status_code == 500 and not error.retryable
    assert error.message == (
        "Autotask zone discovery failed with HTTP 500: Zone information could not be determined. "
        "Check AUTOTASK_USERNAME or set AUTOTASK_BASE_URL"
    )
    assert len(server.calls("autotask_zone")) == 1, "the permanent zone error is not retried"
    assert server.calls("autotask_create") == []


@pytest.mark.parametrize(
    "zone_url", ["http://webservices15.autotask.net/atservicesrest/", "https://evil.example/atservicesrest/"]
)
def test_untrusted_zone_url_is_refused(zone_url: str) -> None:
    adapter, server, _ = make(MockPsaServer(autotask_zone_url=zone_url))
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert "unexpected URL" in caught.value.message
    assert server.calls("autotask_create") == []


def test_zone_discovery_is_retried_on_503() -> None:
    adapter, server, _ = make()
    server.fail_next(503, route="autotask_zone")
    result = adapter.create_ticket(ticket())
    assert len(server.calls("autotask_zone")) == 2
    assert result.attempts == 1, "zone attempts are not create attempts"


# Request body ----------------------------------------------------------------------

def test_request_body_holds_the_documented_fields() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket())
    sent = server.body(server.calls("autotask_create")[0])
    description = sent.pop("description")
    assert sent == {
        "companyID": 0,
        "title": "TKT-1104-0001 Disk space low on SRV-FS01",
        "status": 1,
        "priority": 1,
        "dueDateTime": "2026-11-04T13:00:00Z",
        "externalID": "TKT-1104-0001",
    }
    assert "TriageOps ticket TKT-1104-0001" in description
    assert "draft must never reach" not in description


def test_company_id_zero_is_sent_and_not_dropped() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket())
    sent = server.body(server.calls("autotask_create")[0])
    assert "companyID" in sent and sent["companyID"] == 0


def test_optional_ids_are_sent_when_configured() -> None:
    adapter, server, _ = make(
        AUTOTASK_COMPANY_ID="174", AUTOTASK_QUEUE_ID="29683487", AUTOTASK_SOURCE_ID="0", AUTOTASK_TICKET_TYPE_ID="5",
        AUTOTASK_STATUS_ID="8",
    )  # fmt: skip
    adapter.create_ticket(ticket())
    sent = server.body(server.calls("autotask_create")[0])
    assert (sent["companyID"], sent["queueID"], sent["source"], sent["ticketType"], sent["status"]) == (
        174, 29683487, 0, 5, 8,
    )  # fmt: skip


@pytest.mark.parametrize(("priority", "expected"), [("P1", 4), ("P2", 1), ("P3", 2), ("P4", 3), (None, 2)])
def test_default_priority_picklist_map(priority: str | None, expected: int) -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket(priority=priority))
    assert server.body(server.calls("autotask_create")[0])["priority"] == expected


def test_custom_priority_ids_are_used() -> None:
    adapter, server, _ = make(MockPsaServer(autotask_priorities=(11, 12, 13, 14)), AUTOTASK_PRIORITY_IDS="P1:11,P2:12,P3:13,P4:14")
    adapter.create_ticket(ticket(priority="P1"))
    assert server.body(server.calls("autotask_create")[0])["priority"] == 11


def test_due_date_defaults_to_four_hours_from_now() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket(sla_due_at=None))
    assert server.body(server.calls("autotask_create")[0])["dueDateTime"] == "2026-11-04T13:30:00Z"


def test_title_is_capped_at_255() -> None:
    adapter, server, _ = make()
    adapter.create_ticket(ticket(subject="y" * 400))
    assert len(server.body(server.calls("autotask_create")[0])["title"]) == 255


# Answers ---------------------------------------------------------------------------

def test_item_id_is_the_external_id() -> None:
    adapter, _, _ = make()
    result = adapter.create_ticket(ticket())
    assert (result.provider, result.external_id, result.external_ref, result.url, result.attempts) == (
        "autotask", "29683664", None, None, 1,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("data", "expected"),
    [({"itemId": 5}, "5"), ({"ItemID": "6"}, "6"), ({"item": {"id": 7}}, "7"), ({"id": 8}, "8"), ({"itemId": 0}, None),
     ({}, None), ([], None), (None, None)],
)  # fmt: skip
def test_parse_item_id(data: Any, expected: str | None) -> None:
    assert parse_item_id(data) == expected


def test_answer_without_an_item_id_is_resolved_by_query() -> None:
    adapter, server, _ = make()
    server.fail_next(200, route="autotask_create", body={}, commit=True)
    result = adapter.create_ticket(ticket())
    assert result.external_id == "29683664"
    assert len(server.calls("autotask_create")) == 1


def test_answer_without_an_item_id_and_no_ticket_is_an_error() -> None:
    adapter, server, _ = make()
    server.fail_next(200, route="autotask_create", body={})
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert "may have been created" in caught.value.message
    assert len(server.calls("autotask_create")) == 1


# Errors, retries and lookups ------------------------------------------------------

def test_wrong_credentials_are_a_permanent_401() -> None:
    adapter, server, _ = make(AUTOTASK_SECRET="not-the-secret")
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    error = caught.value
    assert (error.status_code, error.retryable, error.attempts) == (401, False, 1)
    assert error.message.startswith("Autotask rejected the API credentials with HTTP 401")
    assert "not-the-secret" not in error.message


def test_429_then_200_is_retried() -> None:
    adapter, server, clock = make()
    server.fail_next(429, route="autotask_create")
    result = adapter.create_ticket(ticket())
    assert result.attempts == 2 and clock.sleeps == [0.5]


def test_data_violation_500_is_permanent_with_no_retry_and_no_query() -> None:
    adapter, server, _ = make()
    server.fail_next(
        500, route="autotask_create",
        body={"errors": ["Data violation: Alert tickets cannot be associated with a Problem Ticket.."]},
    )  # fmt: skip
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    error = caught.value
    assert error.message == (
        "Autotask rejected the ticket with HTTP 500: "
        "Data violation: Alert tickets cannot be associated with a Problem Ticket.."
    )
    assert not error.retryable
    assert len(server.calls("autotask_create")) == 1 and server.calls("autotask_query") == []


def test_missing_due_date_error_is_permanent() -> None:
    adapter, server, _ = make()
    server.fail_next(500, route="autotask_create", body={"errors": ["DueDateTime is required"]})
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert caught.value.message.endswith("DueDateTime is required")
    assert server.calls("autotask_query") == []


def test_transient_500_without_a_ticket_is_queried_then_resent() -> None:
    adapter, server, _ = make()
    server.fail_next(500, route="autotask_create", body={"errors": ["Transaction deadlocked"]})
    result = adapter.create_ticket(ticket())
    assert result.attempts == 2 and len(server.autotask_tickets) == 1
    assert server.routes == ["autotask_zone", "autotask_create", "autotask_query", "autotask_create"]
    query = server.body(server.calls("autotask_query")[0])
    assert query == {"filter": [{"op": "eq", "field": "externalID", "value": "TKT-1104-0001"}], "maxRecords": 1}
    assert str(server.calls("autotask_query")[0].url) == f"{BASE}/Tickets/query"


def test_transient_500_after_the_ticket_was_created_is_found_by_query() -> None:
    adapter, server, _ = make()
    server.fail_next(504, route="autotask_create", commit=True)
    result = adapter.create_ticket(ticket())
    assert result.external_id == "29683664" and result.attempts == 1
    assert len(server.calls("autotask_create")) == 1 and len(server.autotask_tickets) == 1


def test_read_timeout_after_commit_is_found_by_query() -> None:
    adapter, server, _ = make()
    server.fail_next_transport(httpx.ReadTimeout, route="autotask_create", commit=True)
    assert adapter.create_ticket(ticket()).external_id == "29683664"
    assert len(server.autotask_tickets) == 1


def test_failed_query_says_the_ticket_may_exist() -> None:
    adapter, server, _ = make()
    server.fail_next(502, route="autotask_create", commit=True)
    server.fail_next(500, times=5, route="autotask_query", body={"errors": ["externalID is not queryable"]})
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    error = caught.value
    assert error.ambiguous and not error.retryable and error.status_code == 502
    assert "may already exist in Autotask" in error.message
    assert len(server.calls("autotask_create")) == 1


def test_error_summary_is_bounded_and_holds_no_secrets() -> None:
    adapter, server, _ = make()
    long_error = f"Secret {AUTOTASK_SECRET} and code {AUTOTASK_INTEGRATION_CODE} rejected; " + "detail " * 100
    server.fail_next(400, route="autotask_create", body={"errors": [long_error, {"message": "second", "field": "title"}]})
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    message = caught.value.message
    assert AUTOTASK_SECRET not in message and AUTOTASK_INTEGRATION_CODE not in message
    assert ";" not in message
    summary = message.split(": ", 1)[1]
    assert len(summary) <= 200 and summary.endswith("...")


def test_error_list_items_with_fields_are_summarised() -> None:
    adapter, server, _ = make()
    server.fail_next(400, route="autotask_create", body={"errors": [{"message": "too long", "field": "title"}, "other"]})
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert caught.value.message == "Autotask rejected the ticket with HTTP 400: title: too long | other"


def test_plain_text_and_html_errors() -> None:
    adapter, server, _ = make()
    server.fail_next(400, route="autotask_create", body="Bad request text")
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket())
    assert caught.value.message == "Autotask rejected the ticket with HTTP 400: Bad request text"

    server.fail_next(400, route="autotask_create", body="<html><body>" + "z" * 500 + "</body></html>")
    with pytest.raises(PsaError) as caught:
        adapter.create_ticket(ticket(ref="TKT-1104-0002"))
    assert caught.value.message == "Autotask rejected the ticket with HTTP 400"
