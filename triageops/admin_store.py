"""SQL used only by the analyst console.

Ticket updates and events reuse ``store``. This module adds the read behind the
overrides export, so the shared store module stays as it was. Every value is a
bound parameter.
"""

from typing import Any

from .db import Connection

MAX_EXPORT_ROWS = 10_000


def list_overrides(conn: Connection, limit: int = MAX_EXPORT_ROWS) -> list[dict[str, Any]]:
    """Every human override, oldest first, next to the first decision Claude recorded for the ticket."""
    return conn.execute(
        """
        select
          t.ref, t.channel, t.created_at as ticket_created_at, t.subject, t.body,
          o.id as event_id, o.created_at as overridden_at,
          o.detail->>'field' as field,
          o.detail->>'from' as from_value,
          o.detail->>'to' as to_value,
          o.detail->>'reason' as reason,
          c.detail->>'model' as claude_model,
          c.detail->>'category' as claude_category,
          c.detail->>'priority' as claude_priority,
          c.detail->>'team' as claude_team,
          c.detail->>'confidence' as claude_confidence
        from ticket_events o
        join tickets t on t.id = o.ticket_id
        left join lateral (
          select e.detail from ticket_events e
          where e.ticket_id = o.ticket_id and e.type = 'triaged'
          order by e.id
          limit 1
        ) c on true
        where o.type = 'human_override'
        order by o.id
        limit %s
        """,
        (limit,),
    ).fetchall()
