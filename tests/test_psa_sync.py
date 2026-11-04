"""sync_after_triage end to end through triage.process_ticket, plus the factory and PsaTicket."""

import logging
from typing import Any

import httpx
import pytest
from fakes import FakeStore
from mock_psa import (
    AUTOTASK_SECRET,
    HALO_CLIENT_SECRET,
    TICKET_ROW,
    FakeClock,
    MockPsaServer,
    autotask_settings,
    client_for,
    halo_settings,
)

from triageops import store, triage
from triageops.psa import factory, sync
from triageops.psa.autotask import AutotaskAdapter
from triageops.psa.base import PsaError, PsaResult, PsaTicket
from triageops.psa.config import PsaSettings, psa_settings
from triageops.psa.halopsa import HaloPsaAdapter
from triageops.samples import SAMPLES_BY_KEY
from triageops.schemas import InboundTicket

PRINTER = SAMPLES_BY_KEY["printer"]


def inbound(**overrides: Any) -> InboundTicket:
    fields = {
        "channel": PRINTER.channel, "subject": PRINTER.subject, "body": PRINTER.body,
        "customer": PRINTER.customer, "requester": PRINTER.requester,
    }  # fmt: skip
    return InboundTicket(**{**fields, **overrides})


def psa_events(fake: FakeStore, ticket_id: Any) -> list[dict[str, Any]]:
    return [e for e in fake.list_events(None, ticket_id) if e["type"].startswith("psa_")]


class Wired:
    """Turns PSA sync on for ``process_ticket`` with an adapter wired to the mock server."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, settings: PsaSettings, server: MockPsaServer) -> None:
        self.settings, self.server, self.clock = settings, server, FakeClock()
        kwargs = {"client": client_for(server), "sleep": self.clock.sleep, "clock": self.clock}
        cls = HaloPsaAdapter if settings.provider == "halopsa" else AutotaskAdapter
        self.adapter = cls(settings, **kwargs)
        monkeypatch.setattr(sync, "psa_settings", settings)
        monkeypatch.setattr(factory, "get_adapter", lambda s=None: self.adapter)


@pytest.fixture
def halo(monkeypatch: pytest.MonkeyPatch) -> Wired:
    return Wired(monkeypatch, halo_settings(), MockPsaServer())


@pytest.fixture
def autotask(monkeypatch: pytest.MonkeyPatch) -> Wired:
    return Wired(monkeypatch, autotask_settings(), MockPsaServer())


# Off -------------------------------------------------------------------------------

def test_psa_is_off_in_every_test_by_default() -> None:
    assert psa_settings.provider == ""


def test_disabled_writes_nothing_and_touches_no_store_function(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = triage.process_ticket(None, inbound())
    assert psa_events(fake_store, bundle.ticket["id"]) == []

    touched: list[str] = []
    monkeypatch.setattr(store, "list_events", lambda *a, **k: touched.append("list_events"))
    monkeypatch.setattr(store, "add_events", lambda *a, **k: touched.append("add_events"))
    assert sync.sync_after_triage(None, bundle.ticket) is None
    assert touched == []


# HaloPSA and Autotask end to end -------------------------------------------------

def test_halopsa_enabled_writes_psa_synced_with_the_external_id(fake_store: FakeStore, halo: Wired) -> None:
    bundle = triage.process_ticket(None, inbound())

    assert bundle.ticket["status"] == "triaged"
    (event,) = [e for e in bundle.events if e["type"].startswith("psa_")]
    assert event["type"] == "psa_synced"
    assert event["detail"] == {
        "description": "Created in HaloPSA as 54321",
        "provider": "halopsa",
        "external_id": "54321",
        "external_ref": None,
        "url": "https://acme.halopsa.com/tickets?id=54321",
        "attempts": 1,
    }
    sent = halo.server.body(halo.server.calls("halo_create")[0])[0]
    assert sent["third_party_id_string"] == bundle.ticket["ref"]
    assert sent["summary"] == f"{bundle.ticket['ref']} {bundle.ticket['subject']}"
    assert sent["priority_id"] == int(bundle.ticket["priority"][1])
    assert bundle.ticket["suggested_reply"] not in sent["details"]


def test_autotask_enabled_writes_psa_synced_with_the_item_id(fake_store: FakeStore, autotask: Wired) -> None:
    bundle = triage.process_ticket(None, inbound())
    (event,) = psa_events(fake_store, bundle.ticket["id"])
    assert event["type"] == "psa_synced"
    assert event["detail"]["description"] == "Created in Autotask as 29683664"
    assert (event["detail"]["external_id"], event["detail"]["url"]) == ("29683664", None)
    sent = autotask.server.body(autotask.server.calls("autotask_create")[0])
    assert sent["externalID"] == bundle.ticket["ref"] and sent["companyID"] == 0
    assert sent["dueDateTime"].endswith("Z")


def test_demo_tickets_are_skipped(fake_store: FakeStore, halo: Wired) -> None:
    bundle = triage.process_ticket(None, inbound(is_demo=True))
    (event,) = psa_events(fake_store, bundle.ticket["id"])
    assert event["type"] == "psa_skipped"
    assert event["detail"] == {"provider": "halopsa", "reason": "Demo portal tickets are not sent to the PSA"}
    assert halo.server.requests == []


def test_misconfigured_provider_is_skipped_with_the_missing_names(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = PsaSettings.from_env({"PSA_PROVIDER": "autotask", "AUTOTASK_USERNAME": "api@msp.example",
                                     "AUTOTASK_BASE_URL": "http://webservices1.autotask.net/atservicesrest"})  # fmt: skip
    monkeypatch.setattr(sync, "psa_settings", settings)
    bundle = triage.process_ticket(None, inbound())
    (event,) = psa_events(fake_store, bundle.ticket["id"])
    assert event["type"] == "psa_skipped"
    assert event["detail"]["reason"] == (
        "PSA sync is switched on but these settings are missing: AUTOTASK_SECRET, AUTOTASK_INTEGRATION_CODE, "
        "AUTOTASK_COMPANY_ID, AUTOTASK_BASE_URL (must be an https URL)"
    )


def test_psa_failure_writes_psa_failed_and_the_ticket_is_still_triaged(fake_store: FakeStore, halo: Wired) -> None:
    halo.server.fail_next(400, route="halo_create", body={"[0].tickettype_id": "Ticket type 31 does not exist"})
    bundle = triage.process_ticket(None, inbound())

    assert bundle.ticket["status"] == "triaged" and bundle.ticket["priority"]
    (event,) = psa_events(fake_store, bundle.ticket["id"])
    assert event["type"] == "psa_failed"
    assert event["detail"] == {
        "reason": "HaloPSA rejected the ticket with HTTP 400: [0].tickettype_id: Ticket type 31 does not exist",
        "provider": "halopsa",
        "status_code": 400,
        "attempts": 1,
        "retryable": False,
    }


def test_unreachable_psa_is_recorded_as_retryable(fake_store: FakeStore, halo: Wired) -> None:
    halo.server.fail_next_transport(httpx.ConnectError, times=10, route="halo_token")
    bundle = triage.process_ticket(None, inbound())
    (event,) = psa_events(fake_store, bundle.ticket["id"])
    assert event["type"] == "psa_failed" and event["detail"]["retryable"] is True
    assert event["detail"]["reason"] == "HaloPSA could not be reached (ConnectError)"
    assert halo.clock.sleeps == [0.5, 1.0]


def test_ambiguous_failure_tells_a_person_to_check_the_psa(fake_store: FakeStore, autotask: Wired) -> None:
    autotask.server.fail_next(502, route="autotask_create", commit=True)
    autotask.server.fail_next(500, times=5, route="autotask_query")
    bundle = triage.process_ticket(None, inbound())
    (event,) = psa_events(fake_store, bundle.ticket["id"])
    assert event["type"] == "psa_failed"
    assert "may already exist in Autotask" in event["detail"]["reason"]
    assert event["detail"]["retryable"] is False


def test_second_call_is_idempotent(fake_store: FakeStore, halo: Wired) -> None:
    bundle = triage.process_ticket(None, inbound())
    assert sync.sync_after_triage(None, bundle.ticket) is None
    assert [e["type"] for e in psa_events(fake_store, bundle.ticket["id"])] == ["psa_synced"]
    assert len(halo.server.calls("halo_create")) == 1


def test_failed_ticket_is_not_sent(fake_store: FakeStore, halo: Wired) -> None:
    ticket = fake_store.insert_ticket(None, channel="email", customer=None, requester=None, subject="Printer down",
                                      body="The printer is down.", pii_masked=False, client_hash=None, is_demo=False)  # fmt: skip
    ticket["status"] = "failed"
    assert sync.sync_after_triage(None, ticket) is None
    assert fake_store.events == [] and halo.server.requests == []


def test_returns_the_result_on_success(fake_store: FakeStore, halo: Wired) -> None:
    ticket = fake_store.insert_ticket(None, channel="email", customer=None, requester=None, subject="Printer down",
                                      body="The printer is down.", pii_masked=False, client_hash=None, is_demo=False)  # fmt: skip
    result = sync.sync_after_triage(None, ticket)
    assert result == PsaResult("halopsa", "54321", None, "https://acme.halopsa.com/tickets?id=54321", 1)


# Never raises --------------------------------------------------------------------

class Exploding:
    name = "halopsa"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def create_ticket(self, ticket: PsaTicket) -> PsaResult:
        raise self.exc


def test_unexpected_exception_is_recorded_by_type_only(fake_store: FakeStore, caplog: pytest.LogCaptureFixture) -> None:
    ticket = fake_store.insert_ticket(None, channel="email", customer=None, requester=None, subject="Printer down",
                                      body="The printer is down.", pii_masked=False, client_hash=None, is_demo=False)  # fmt: skip
    with caplog.at_level(logging.DEBUG):
        result = sync.sync_after_triage(
            None, ticket, settings=halo_settings(), adapter=Exploding(ValueError("body text and token xyz"))
        )
    assert result is None
    (event,) = psa_events(fake_store, ticket["id"])
    assert event["type"] == "psa_failed"
    assert event["detail"] == {
        "reason": "PSA sync failed unexpectedly (ValueError)", "provider": "halopsa", "status_code": None,
        "attempts": 0, "retryable": False,
    }  # fmt: skip
    assert "body text" not in caplog.text and "token xyz" not in caplog.text


def test_psa_error_from_an_injected_adapter(fake_store: FakeStore) -> None:
    ticket = fake_store.insert_ticket(None, channel="email", customer=None, requester=None, subject="Printer down",
                                      body="The printer is down.", pii_masked=False, client_hash=None, is_demo=False)  # fmt: skip
    error = PsaError("Autotask rejected the ticket with HTTP 429", status_code=429, attempts=3, retryable=True)
    sync.sync_after_triage(None, ticket, settings=autotask_settings(), adapter=Exploding(error))
    (event,) = psa_events(fake_store, ticket["id"])
    assert event["detail"]["provider"] == "autotask"
    assert (event["detail"]["status_code"], event["detail"]["attempts"], event["detail"]["retryable"]) == (429, 3, True)


def test_event_write_failure_does_not_raise(fake_store: FakeStore, halo: Wired, monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = fake_store.insert_ticket(None, channel="email", customer=None, requester=None, subject="Printer down",
                                      body="The printer is down.", pii_masked=False, client_hash=None, is_demo=False)  # fmt: skip

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr(store, "add_events", broken)
    assert sync.sync_after_triage(None, ticket) is not None
    monkeypatch.setattr(store, "list_events", broken)
    assert sync.sync_after_triage(None, ticket) is None


def test_logs_hold_no_secrets_tokens_or_ticket_bodies(
    fake_store: FakeStore, halo: Wired, caplog: pytest.LogCaptureFixture
) -> None:
    halo.server.fail_next(500, route="halo_create")
    with caplog.at_level(logging.DEBUG):
        bundle = triage.process_ticket(None, inbound())
        halo.server.expire_tokens()
        halo.server.fail_next(400, route="halo_create", body={"message": f"echo {HALO_CLIENT_SECRET}"})
        second = triage.process_ticket(None, inbound(subject="Second printer fault", body="Another printer is offline."))
    assert [e["type"] for e in psa_events(fake_store, bundle.ticket["id"])] == ["psa_synced"]
    assert [e["type"] for e in psa_events(fake_store, second.ticket["id"])] == ["psa_failed"]
    for secret in (HALO_CLIENT_SECRET, AUTOTASK_SECRET, *halo.server.tokens_issued, PRINTER.body[:40]):
        assert secret not in caplog.text
    for event in fake_store.events:
        assert HALO_CLIENT_SECRET not in str(event["detail"])


# Factory ---------------------------------------------------------------------------

@pytest.fixture
def clean_factory() -> Any:
    factory.reset_adapter_cache()
    yield
    factory.reset_adapter_cache()


def test_factory_returns_none_when_off_or_not_ready(clean_factory: None) -> None:
    assert factory.get_adapter() is None, "the conftest fixture switches PSA off"
    assert factory.get_adapter(PsaSettings.from_env({"PSA_PROVIDER": "halopsa"})) is None


def test_factory_builds_and_caches_per_provider(clean_factory: None) -> None:
    halo_config, autotask_config = halo_settings(), autotask_settings()
    first = factory.get_adapter(halo_config)
    assert isinstance(first, HaloPsaAdapter)
    assert factory.get_adapter(halo_config) is first
    assert factory.get_adapter(halo_settings()) is first, "equal settings share the adapter and its token cache"
    assert isinstance(factory.get_adapter(autotask_config), AutotaskAdapter)

    changed = halo_settings(HALOPSA_TEAM_ID="4")
    assert factory.get_adapter(changed) is not first

    factory.reset_adapter_cache()
    assert first.client.is_closed
    assert factory.get_adapter(halo_config) is not first


# PsaTicket -------------------------------------------------------------------------

def test_psa_ticket_parses_strings_and_never_holds_the_reply() -> None:
    ticket = PsaTicket.from_ticket(
        {**TICKET_ROW, "sla_due_at": "2026-11-04T13:00:00+00:00", "created_at": "2026-11-04T09:00:00Z",
         "next_steps": '["Check the volume", ""]', "injection_suspected": None}
    )  # fmt: skip
    assert ticket.sla_due_at is not None and ticket.sla_due_at.hour == 13
    assert ticket.created_at is not None and ticket.created_at.tzinfo is not None
    assert ticket.next_steps == ("Check the volume",)
    assert ticket.injection_suspected is False
    assert not hasattr(ticket, "suggested_reply")
    assert TICKET_ROW["suggested_reply"] not in ticket.description()


def test_psa_ticket_description_is_bounded_and_plain() -> None:
    ticket = PsaTicket.from_ticket({**TICKET_ROW, "body": "word " * 3000, "escalation_reason": "P1 security incident"})
    text = ticket.description()
    assert len(text) <= 8000 and text.endswith("[truncated]")
    assert "Escalation: P1 security incident" in text
    assert ";" not in text.replace(TICKET_ROW["body"], "")
