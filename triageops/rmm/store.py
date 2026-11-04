"""SQL for RMM alert de-duplication, in the style of ``triageops.store``.

The de-duplication key and the delivery id live in the ``detail`` of
``received`` and ``rmm_repeat`` events. Migration 004 adds partial expression
indexes on ``detail->>'rmm_key'`` and ``detail->>'source_id'`` for exactly these
event types, so every lookup here repeats the same literal type list (the
planner can only use a partial index when the query implies its predicate).
Ticket columns are prefixed with the table alias, and every value is a bound
parameter.
"""

from typing import Any

from psycopg import sql

from ..db import Connection
from ..store import TICKET_COLUMNS
from ..taxonomy import OPEN_STATUSES

# Reset handling looks back this far for the ticket a cleared alert belongs to.
RESET_LOOKBACK_DAYS = 7

_T_FIELDS = sql.SQL(", ").join(sql.Identifier("t", column) for column in TICKET_COLUMNS)

FIND_ALERT_DELIVERY = sql.SQL(
    "select {fields} from tickets t where t.id = ("
    "select e.ticket_id from ticket_events e "
    "where e.type in ('received', 'rmm_repeat') and e.detail->>'source_id' = %s "
    "order by e.id limit 1)"
).format(fields=_T_FIELDS)

# Rolling window: measured from the newest occurrence, so a flapping alert stays on one ticket.
FIND_RECENT_ALERT_TICKET = sql.SQL(
    "select {fields} from ticket_events e join tickets t on t.id = e.ticket_id "
    "where e.type in ('received', 'rmm_repeat') and e.detail->>'rmm_key' = %s "
    "and e.created_at > now() - make_interval(mins => %s::int) "
    "and t.status = any(%s) "
    "order by e.created_at desc, e.id desc limit 1"
).format(fields=_T_FIELDS)

COUNT_ALERT_OCCURRENCES = sql.SQL(
    "select count(*) as n from ticket_events e "
    "where e.ticket_id = %s and e.type in ('received', 'rmm_repeat') and e.detail->>'rmm_key' = %s"
)

FIND_OPEN_ALERT_TICKET = sql.SQL(
    "select {fields} from ticket_events e join tickets t on t.id = e.ticket_id "
    "where e.type in ('received', 'rmm_repeat') and e.detail->>'rmm_key' = %s "
    "and e.created_at > now() - make_interval(days => %s::int) "
    "and t.status = any(%s) "
    "order by e.created_at desc, e.id desc limit 1"
).format(fields=_T_FIELDS)


def _open_statuses() -> list[str]:
    # Resolved tickets are never reused. Failed tickets are not either, so a repeat gets a fresh triage.
    return list(OPEN_STATUSES)


def find_alert_delivery(conn: Connection, source_id: str) -> dict[str, Any] | None:
    """The ticket whose ``received`` or ``rmm_repeat`` event carries this delivery id."""
    return conn.execute(FIND_ALERT_DELIVERY, (source_id,)).fetchone()


def find_recent_alert_ticket(conn: Connection, rmm_key: str, within_minutes: int) -> dict[str, Any] | None:
    """The open ticket of the newest occurrence of ``rmm_key`` within the last ``within_minutes``."""
    return conn.execute(FIND_RECENT_ALERT_TICKET, (rmm_key, within_minutes, _open_statuses())).fetchone()


def count_alert_occurrences(conn: Connection, ticket_id: Any, rmm_key: str) -> int:
    """How many ``received`` and ``rmm_repeat`` events for ``rmm_key`` the ticket holds."""
    return conn.execute(COUNT_ALERT_OCCURRENCES, (ticket_id, rmm_key)).fetchone()["n"]


def find_open_alert_ticket(conn: Connection, rmm_key: str) -> dict[str, Any] | None:
    """The open ticket most recently hit by ``rmm_key`` in the last seven days, for reset handling."""
    return conn.execute(FIND_OPEN_ALERT_TICKET, (rmm_key, RESET_LOOKBACK_DAYS, _open_statuses())).fetchone()
