"""Decide what one RMM alert does: create a ticket, collapse into one, record a reset or nothing.

The order of the checks matters.

1. A delivery id that was already stored is a retried delivery. It returns the
   original ticket and never counts as a new occurrence.
2. A cleared alert adds an ``rmm_reset`` event to the open ticket for the same
   device and condition. The ticket status is left alone, a person decides.
3. A repeat of the same device and condition within the rolling window adds an
   ``rmm_repeat`` event to the open ticket. There is no new triage, no new PSA
   ticket and no new alert.
4. Anything else is triaged as a new ticket.

Repeat and reset events never hold the raw alert message, because the ticket
already holds the masked body.
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal

from .. import store, triage
from ..db import Connection
from . import store as rmm_store
from .config import clamp_minutes, rmm_settings
from .models import RmmAlert, priority_hint
from .tickets import to_inbound

logger = logging.getLogger(__name__)

Action = Literal["created", "collapsed", "duplicate", "reset", "ignored"]

RESET_DESCRIPTION = "The RMM reported that the alert condition has cleared"
NO_OPEN_TICKET_REASON = "No open ticket matches this cleared alert"


@dataclass(frozen=True)
class RmmOutcome:
    action: Action
    ticket: dict[str, Any] | None
    occurrences: int
    priority_hint: str | None
    # Set when nothing was done, so the caller can see why.
    reason: str | None = None


def _occurrences(conn: Connection, ticket: dict[str, Any], alert: RmmAlert) -> int:
    return rmm_store.count_alert_occurrences(conn, ticket["id"], alert.dedupe_key)


def _reread(conn: Connection, ticket: dict[str, Any]) -> dict[str, Any]:
    return store.get_ticket(conn, str(ticket["id"])) or ticket


def _reset_already_recorded(conn: Connection, ticket: dict[str, Any], source_id: str | None) -> bool:
    if not source_id:
        return False
    return any(
        event["type"] == "rmm_reset" and (event.get("detail") or {}).get("source_id") == source_id
        for event in store.list_events(conn, ticket["id"])
    )


def _handle_reset(conn: Connection, alert: RmmAlert, hint: str | None) -> RmmOutcome:
    ticket = rmm_store.find_open_alert_ticket(conn, alert.dedupe_key)
    if ticket is None:
        return RmmOutcome("ignored", None, 0, hint, reason=NO_OPEN_TICKET_REASON)
    occurrences = _occurrences(conn, ticket, alert)
    if _reset_already_recorded(conn, ticket, alert.source_id):
        return RmmOutcome("duplicate", ticket, occurrences, hint)
    detail = {
        "description": RESET_DESCRIPTION,
        "rmm_key": alert.dedupe_key,
        "source_id": alert.source_id,
        "severity": alert.severity,
    }
    store.add_events(conn, ticket["id"], [("rmm_reset", detail)])
    return RmmOutcome("reset", ticket, occurrences, hint)


def _collapse(conn: Connection, ticket: dict[str, Any], alert: RmmAlert, hint: str | None) -> RmmOutcome:
    occurrences = _occurrences(conn, ticket, alert) + 1
    detail = {
        "description": f"Repeat alert collapsed into this ticket, occurrence {occurrences}",
        "rmm_key": alert.dedupe_key,
        "source_id": alert.source_id,
        "occurrences": occurrences,
        "severity": alert.severity,
        "occurred_at": alert.occurred_at.isoformat() if alert.occurred_at else None,
    }
    store.add_events(conn, ticket["id"], [("rmm_repeat", detail)])
    return RmmOutcome("collapsed", _reread(conn, ticket), occurrences, hint)


def handle_alert(
    conn: Connection, alert: RmmAlert, *, client_hash: str | None, dedupe_minutes: int | None = None
) -> RmmOutcome:
    """Apply the checks in order and return what happened. Database errors propagate."""
    hint = priority_hint(alert)
    if alert.source_id:
        delivered = rmm_store.find_alert_delivery(conn, alert.source_id)
        if delivered:
            return RmmOutcome("duplicate", delivered, _occurrences(conn, delivered, alert), hint)

    if alert.state == "reset":
        return _log(_handle_reset(conn, alert, hint), alert)

    minutes = clamp_minutes(rmm_settings.dedupe_minutes if dedupe_minutes is None else dedupe_minutes)
    recent = rmm_store.find_recent_alert_ticket(conn, alert.dedupe_key, minutes)
    if recent:
        return _log(_collapse(conn, recent, alert, hint), alert)

    bundle = triage.process_ticket(conn, to_inbound(alert, client_hash=client_hash))
    if bundle.duplicate:
        return RmmOutcome("duplicate", bundle.ticket, _occurrences(conn, bundle.ticket, alert), hint)
    return _log(RmmOutcome("created", bundle.ticket, 1, hint), alert)


def _log(outcome: RmmOutcome, alert: RmmAlert) -> RmmOutcome:
    ref = outcome.ticket.get("ref") if outcome.ticket else None
    logger.info(
        "rmm alert vendor=%s action=%s ref=%s occurrences=%d", alert.vendor, outcome.action, ref, outcome.occurrences
    )
    return outcome
