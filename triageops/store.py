"""Every SQL statement the backend runs, as small functions that take a connection.

Keeping all queries in one module makes data access easy to review and lets the
tests replace this layer with an in-memory fake. Identifiers are composed with
``psycopg.sql`` and every value is a bound parameter.
"""

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any

from psycopg import errors, sql
from psycopg.types.json import Jsonb

from .db import Connection
from .taxonomy import CATEGORIES, CHANNELS, OPEN_STATUSES, PRIORITIES, STATUSES

# Every ticket column except client_hash, which never leaves the database.
TICKET_COLUMNS: tuple[str, ...] = (
    "id", "ref", "created_at", "updated_at", "channel", "customer", "requester",
    "subject", "body", "status", "category", "priority", "sentiment", "team",
    "summary", "suggested_reply", "next_steps", "kb_refs", "confidence",
    "injection_suspected", "pii_masked", "escalation_reason", "sla_due_at",
    "sla_breached", "ai_model", "ai_latency_ms", "input_tokens", "output_tokens",
    "is_demo",
)  # fmt: skip
_FIELDS = sql.SQL(", ").join(map(sql.Identifier, TICKET_COLUMNS))

_UPDATABLE = frozenset(
    {
        "status", "category", "priority", "sentiment", "team", "summary",
        "suggested_reply", "next_steps", "kb_refs", "confidence",
        "injection_suspected", "escalation_reason", "sla_due_at", "sla_breached",
        "ai_model", "ai_latency_ms", "input_tokens", "output_tokens",
    }
)  # fmt: skip
_JSON_COLUMNS = frozenset({"next_steps", "kb_refs"})

# The ref default is random within a day, so a collision is possible. Retry it.
_REF_ATTEMPTS = 4

_jsonb = partial(Jsonb, dumps=partial(json.dumps, default=str))


# Serialisation -----------------------------------------------------------------

def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def to_json(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a database row into JSON friendly values."""
    return {key: _json_value(value) for key, value in row.items()}


# Tickets -----------------------------------------------------------------------

def insert_ticket(
    conn: Connection,
    *,
    channel: str,
    customer: str | None,
    requester: str | None,
    subject: str,
    body: str,
    pii_masked: bool,
    client_hash: str | None,
    is_demo: bool,
) -> dict[str, Any]:
    query = sql.SQL(
        "insert into tickets (channel, customer, requester, subject, body, pii_masked, client_hash, is_demo) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s) returning {fields}"
    ).format(fields=_FIELDS)
    params = (channel, customer, requester, subject, body, pii_masked, client_hash, is_demo)
    attempts_left = _REF_ATTEMPTS
    while True:
        try:
            return conn.execute(query, params).fetchone()
        except errors.UniqueViolation:
            attempts_left -= 1
            if not attempts_left:
                raise


def update_ticket(conn: Connection, ticket_id: Any, fields: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"columns cannot be updated: {sorted(unknown)}")
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(column)) for column in fields
    )
    values = [_jsonb(value) if column in _JSON_COLUMNS else value for column, value in fields.items()]
    query = sql.SQL("update tickets set {assignments} where id = %s returning {fields}").format(
        assignments=assignments, fields=_FIELDS
    )
    return conn.execute(query, [*values, ticket_id]).fetchone()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def get_ticket(conn: Connection, key: str) -> dict[str, Any] | None:
    """Fetch a ticket by uuid or by ref (case insensitive)."""
    key = key.strip()
    column, value = ("id", key) if _is_uuid(key) else ("ref", key.upper())
    query = sql.SQL("select {fields} from tickets where {column} = %s").format(
        fields=_FIELDS, column=sql.Identifier(column)
    )
    return conn.execute(query, (value,)).fetchone()


def find_ticket_by_source(conn: Connection, source_id: str) -> dict[str, Any] | None:
    """Find a ticket created from a provider message id, so retried deliveries are idempotent."""
    query = sql.SQL(
        "select {fields} from tickets where id = ("
        "select ticket_id from ticket_events where type = 'received' and detail->>'source_id' = %s "
        "order by id limit 1)"
    ).format(fields=_FIELDS)
    return conn.execute(query, (source_id,)).fetchone()


def mark_failed(conn: Connection, ticket_id: Any) -> None:
    conn.execute("update tickets set status = 'failed' where id = %s", (ticket_id,))


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_tickets(
    conn: Connection,
    *,
    status: str | None = None,
    priority: str | None = None,
    channel: str | None = None,
    category: str | None = None,
    customer: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Newest first, with optional exact filters and a free text search."""
    conditions: list[sql.Composable] = []
    params: list[Any] = []
    for column, value in (("status", status), ("priority", priority), ("channel", channel), ("category", category)):
        if value:
            conditions.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
            params.append(value)
    if customer:
        conditions.append(sql.SQL("customer ilike %s"))
        params.append(_like_pattern(customer))
    if query:
        searched = ("ref", "subject", "summary", "customer", "body")
        conditions.append(
            sql.SQL("({})").format(
                sql.SQL(" or ").join(sql.SQL("{} ilike %s").format(sql.Identifier(c)) for c in searched)
            )
        )
        params.extend([_like_pattern(query)] * len(searched))

    where = sql.SQL(" where ") + sql.SQL(" and ").join(conditions) if conditions else sql.SQL("")
    statement = sql.SQL("select {fields} from tickets{where} order by created_at desc limit %s").format(
        fields=_FIELDS, where=where
    )
    return conn.execute(statement, [*params, limit]).fetchall()


def queue_stats(conn: Connection) -> dict[str, Any]:
    totals = conn.execute(
        """
        select
          count(*) as total,
          count(*) filter (where status = any(%s)) as open,
          count(*) filter (where status = any(%s) and priority = 'P1') as open_p1,
          count(*) filter (where sla_breached) as sla_breached,
          count(*) filter (where injection_suspected) as injection_flagged,
          count(*) filter (where created_at > now() - interval '24 hours') as last_24h,
          coalesce(round(avg(ai_latency_ms) filter (
            where ai_model is distinct from 'rules-fallback'
          )), 0)::int as avg_latency_ms
        from tickets
        """,
        (list(OPEN_STATUSES), list(OPEN_STATUSES)),
    ).fetchone()
    groups = conn.execute(
        """
        select 'priority' as dimension, priority as value, count(*) as n from tickets
          where priority is not null group by priority
        union all
        select 'category', category, count(*) from tickets where category is not null group by category
        union all
        select 'status', status, count(*) from tickets group by status
        union all
        select 'channel', channel, count(*) from tickets group by channel
        """
    ).fetchall()

    # Every known key is present, so the dashboard can chart zeros without guessing.
    breakdown = {
        "priority": dict.fromkeys(PRIORITIES, 0),
        "category": dict.fromkeys(CATEGORIES, 0),
        "status": dict.fromkeys(STATUSES, 0),
        "channel": dict.fromkeys(CHANNELS, 0),
    }
    for row in groups:
        breakdown[row["dimension"]][row["value"]] = row["n"]

    return {
        "total": totals["total"],
        "open": totals["open"],
        "open_p1": totals["open_p1"],
        "by_priority": breakdown["priority"],
        "by_category": breakdown["category"],
        "by_status": breakdown["status"],
        "by_channel": breakdown["channel"],
        "avg_latency_ms": totals["avg_latency_ms"],
        "sla_breached": totals["sla_breached"],
        "injection_flagged": totals["injection_flagged"],
        "last_24h": totals["last_24h"],
    }


def count_recent_tickets_from(conn: Connection, client_hash: str) -> int:
    row = conn.execute(
        "select count(*) as n from tickets where client_hash = %s and created_at > now() - interval '1 hour'",
        (client_hash,),
    ).fetchone()
    return row["n"]


def count_demo_tickets_today(conn: Connection) -> int:
    row = conn.execute(
        "select count(*) as n from tickets where is_demo and created_at >= date_trunc('day', now())"
    ).fetchone()
    return row["n"]


def mark_sla_breaches(conn: Connection) -> tuple[int, list[dict[str, Any]]]:
    """Flag open tickets past their SLA. Returns (open tickets checked, newly breached rows)."""
    open_statuses = list(OPEN_STATUSES)
    checked = conn.execute(
        "select count(*) as n from tickets where status = any(%s) and not sla_breached and sla_due_at is not null",
        (open_statuses,),
    ).fetchone()["n"]
    breached = conn.execute(
        sql.SQL(
            "update tickets set sla_breached = true "
            "where status = any(%s) and not sla_breached and sla_due_at < now() returning {fields}"
        ).format(fields=_FIELDS),
        (open_statuses,),
    ).fetchall()
    return checked, breached


# Events ------------------------------------------------------------------------

def add_events(conn: Connection, ticket_id: Any, events: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
    """Insert events in one statement. VALUES order sets the identity order."""
    if not events:
        return
    rows = sql.SQL(", ").join(sql.SQL("(%s, %s, %s)") for _ in events)
    params = [value for kind, detail in events for value in (ticket_id, kind, _jsonb(dict(detail)))]
    conn.execute(sql.SQL("insert into ticket_events (ticket_id, type, detail) values {}").format(rows), params)


def list_events(conn: Connection, ticket_id: Any) -> list[dict[str, Any]]:
    return conn.execute(
        "select id, created_at, type, detail from ticket_events where ticket_id = %s order by id",
        (ticket_id,),
    ).fetchall()


# Knowledge base ----------------------------------------------------------------

def kb_count(conn: Connection) -> int:
    return conn.execute("select count(*) as n from kb_articles").fetchone()["n"]


def list_kb_articles(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute("select slug, title, category from kb_articles order by category, title").fetchall()


def existing_kb_slugs(conn: Connection) -> set[str]:
    return {row["slug"] for row in conn.execute("select slug from kb_articles").fetchall()}


def try_advisory_xact_lock(conn: Connection, key: int) -> bool:
    return conn.execute("select pg_try_advisory_xact_lock(%s) as locked", (key,)).fetchone()["locked"]


def insert_kb_articles(conn: Connection, rows: Sequence[tuple[str, str, str, str, str]]) -> int:
    """Insert (slug, title, category, content, vector literal) rows, skipping existing slugs."""
    if not rows:
        return 0
    values = sql.SQL(", ").join(sql.SQL("(%s, %s, %s, %s, %s::extensions.vector)") for _ in rows)
    statement = sql.SQL(
        "insert into kb_articles (slug, title, category, content, embedding) values {} "
        "on conflict (slug) do nothing"
    ).format(values)
    cursor = conn.execute(statement, [value for row in rows for value in row])
    return cursor.rowcount


def match_kb(conn: Connection, vector_literal: str, count: int) -> list[dict[str, Any]]:
    return conn.execute(
        "select slug, title, category, content, similarity from match_kb(%s::extensions.vector, %s::int)",
        (vector_literal, count),
    ).fetchall()


def search_kb_text(conn: Connection, query: str, limit: int) -> list[dict[str, Any]]:
    """Full text search, used by the copilot tool when embeddings are unavailable."""
    words = re.findall(r"[A-Za-z0-9]+", query)[:12]
    if not words:
        return []
    return conn.execute(
        """
        select slug, title, category, content, ts_rank(document, terms) as rank
        from kb_articles,
             to_tsvector('english', title || ' ' || content) as document,
             websearch_to_tsquery('english', %s) as terms
        where document @@ terms
        order by rank desc
        limit %s
        """,
        (" or ".join(words), limit),
    ).fetchall()
