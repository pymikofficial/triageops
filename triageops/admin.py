"""The analyst console API. Every route needs the ``ADMIN_TOKEN`` bearer token.

Claude proposes, a human decides. An analyst can approve the draft reply,
override the priority or team with a reason, resolve a ticket and reopen it.
Each action writes an audit event to the ticket timeline, and every override
is exported next to Claude's original decision as a gold label for evaluation.
Nothing here sends a message to a customer.

Text typed by an analyst goes through the same masking as ticket text before
it is stored, because the timeline is shown on the public dashboard.
"""

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import admin_store, db, store
from .admin_auth import require_admin
from .db import Connection
from .security import mask_text
from .taxonomy import SLA_HOURS, STATUSES, Priority, Team

ACTOR = "analyst"
RESOLVED = "resolved"
REOPEN_DEFAULT_STATUS = "triaged"

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


# Request bodies --------------------------------------------------------------------

class _AdminRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class ReplyApprovalIn(_AdminRequest):
    text: str = Field(min_length=1, max_length=6000)


class OverrideIn(_AdminRequest):
    priority: Priority | None = None
    team: Team | None = None
    reason: str = Field(min_length=3, max_length=500)


class ResolveIn(_AdminRequest):
    note: str = Field(min_length=3, max_length=2000)


class ReopenIn(_AdminRequest):
    note: str | None = Field(default=None, max_length=2000)


TicketKey = Path(min_length=3, max_length=64)


# Helpers ---------------------------------------------------------------------------

def _masked(text: str) -> str:
    return mask_text(text).text


def _load(conn: Connection, ticket_key: str) -> dict[str, Any]:
    ticket = store.get_ticket(conn, ticket_key)
    if ticket is None:
        raise HTTPException(404, "Ticket not found.")
    return ticket


def _require_open(ticket: dict[str, Any]) -> None:
    if ticket["status"] == RESOLVED:
        raise HTTPException(409, "This ticket is resolved. Reopen it first.")


def _bundle(conn: Connection, ticket: dict[str, Any]) -> dict[str, Any]:
    events = store.list_events(conn, ticket["id"])
    return {"ticket": store.to_json(ticket), "events": [store.to_json(event) for event in events]}


def _status_before_resolution(conn: Connection, ticket: dict[str, Any]) -> str:
    for event in reversed(store.list_events(conn, ticket["id"])):
        if event["type"] == RESOLVED:
            previous = (event.get("detail") or {}).get("from")
            if previous in STATUSES and previous != RESOLVED:
                return previous
            break
    return REOPEN_DEFAULT_STATUS


def _sla_outcome(ticket: dict[str, Any]) -> str:
    if ticket.get("sla_due_at") is None:
        return "not set"
    return "breached" if ticket.get("sla_breached") else "met"


# Routes ----------------------------------------------------------------------------

@router.post("/login")
def login() -> dict[str, bool]:
    """Lets the login screen check a token. The dependency has already done the work."""
    return {"ok": True}


@router.post("/tickets/{ticket_key}/reply")
def approve_reply(payload: ReplyApprovalIn, ticket_key: str = TicketKey) -> dict[str, Any]:
    text = _masked(payload.text)
    with db.connect() as conn:
        ticket = _load(conn, ticket_key)
        _require_open(ticket)
        edited = text != (ticket.get("suggested_reply") or "").strip()
        store.add_events(
            conn,
            ticket["id"],
            [
                (
                    "reply_approved",
                    {
                        "text": text,
                        "edited": edited,
                        "delivery": "Recorded only. Nothing was sent to the customer.",
                        "by": ACTOR,
                    },
                )
            ],
        )
        return _bundle(conn, ticket)


@router.post("/tickets/{ticket_key}/override")
def override(payload: OverrideIn, ticket_key: str = TicketKey) -> dict[str, Any]:
    reason = _masked(payload.reason)
    with db.connect() as conn:
        ticket = _load(conn, ticket_key)
        _require_open(ticket)
        changes: dict[str, Any] = {}
        events: list[tuple[str, dict[str, Any]]] = []
        for field, value in (("priority", payload.priority), ("team", payload.team)):
            if value is None or value == ticket.get(field):
                continue
            changes[field] = value
            detail: dict[str, Any] = {"field": field, "from": ticket.get(field), "to": value, "reason": reason, "by": ACTOR}
            if field == "priority":
                # The SLA follows the new priority, counted from when the ticket arrived.
                due = ticket["created_at"] + timedelta(hours=SLA_HOURS[value])
                changes["sla_due_at"] = due
                # A breach the sweep already recorded stands while the new deadline is also past.
                changes["sla_breached"] = bool(ticket.get("sla_breached")) and due <= datetime.now(UTC)
                detail["sla_due_at"] = due.isoformat()
            events.append(("human_override", detail))
        if not changes:
            raise HTTPException(422, "Choose a priority or team that differs from the current one.")
        updated = store.update_ticket(conn, ticket["id"], changes)
        store.add_events(conn, ticket["id"], events)
        return _bundle(conn, updated)


@router.post("/tickets/{ticket_key}/resolve")
def resolve(payload: ResolveIn, ticket_key: str = TicketKey) -> dict[str, Any]:
    note = _masked(payload.note)
    with db.connect() as conn:
        ticket = _load(conn, ticket_key)
        _require_open(ticket)
        updated = store.update_ticket(conn, ticket["id"], {"status": RESOLVED})
        store.add_events(
            conn,
            ticket["id"],
            [
                (
                    RESOLVED,
                    {"note": note, "from": ticket["status"], "to": RESOLVED, "sla": _sla_outcome(ticket), "by": ACTOR},
                )
            ],
        )
        return _bundle(conn, updated)


@router.post("/tickets/{ticket_key}/reopen")
def reopen(payload: ReopenIn | None = None, ticket_key: str = TicketKey) -> dict[str, Any]:
    note = _masked(payload.note) if payload and payload.note else None
    with db.connect() as conn:
        ticket = _load(conn, ticket_key)
        if ticket["status"] != RESOLVED:
            raise HTTPException(409, "Only a resolved ticket can be reopened.")
        status = _status_before_resolution(conn, ticket)
        updated = store.update_ticket(conn, ticket["id"], {"status": status})
        detail: dict[str, Any] = {"from": RESOLVED, "to": status, "by": ACTOR}
        if note:
            detail["note"] = note
        store.add_events(conn, ticket["id"], [("reopened", detail)])
        return _bundle(conn, updated)


# Overrides export ------------------------------------------------------------------

CSV_COLUMNS: tuple[str, ...] = (
    "ticket_ref", "channel", "ticket_created_at", "overridden_at", "field",
    "claude_value", "from_value", "to_value", "reason",
    "claude_model", "claude_category", "claude_priority", "claude_team", "claude_confidence",
    "subject", "body",
)  # fmt: skip

# Spreadsheet apps run a cell that starts with one of these as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def overrides_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        field = row.get("field") or ""
        record = {
            **row,
            "ticket_ref": row.get("ref"),
            "claude_value": row.get(f"claude_{field}") if field in ("priority", "team") else None,
        }
        writer.writerow([_cell(record.get(column)) for column in CSV_COLUMNS])
    return buffer.getvalue()


@router.get("/overrides.csv")
def export_overrides() -> Response:
    with db.connect() as conn:
        rows = admin_store.list_overrides(conn)
    filename = f"triageops-overrides-{datetime.now(UTC):%Y%m%d}.csv"
    return Response(
        overrides_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
