"""Create the triaged ticket in the PSA and record the outcome as an event.

``triage.process_ticket`` calls ``sync_after_triage`` after a successful triage.
It never raises: the pipeline must not fail because of the PSA. Every outcome
is written as one event, except when PSA sync is off (nothing is written and
no store function is called) and when the ticket failed triage or was already
synced (nothing is written, so a repeated call is harmless).

* ``psa_synced``: created, with the PSA id, a link when one is known and the attempts.
* ``psa_skipped``: switched on but misconfigured, or a demo portal ticket.
* ``psa_failed``: the PSA call failed. The reason is safe to show and never
  holds a secret, a token or the ticket body.
"""

import logging
from collections.abc import Mapping
from typing import Any

from .. import store
from . import factory
from .base import PsaAdapter, PsaError, PsaResult, PsaTicket
from .config import PROVIDER_LABELS, PsaSettings, psa_settings

logger = logging.getLogger(__name__)

DEMO_REASON = "Demo portal tickets are not sent to the PSA"


def sync_after_triage(
    conn: Any,
    ticket: Mapping[str, Any],
    *,
    is_demo: bool = False,
    settings: PsaSettings | None = None,
    adapter: PsaAdapter | None = None,
) -> PsaResult | None:
    """Send ``ticket`` to the configured PSA. Returns the result, or None when nothing was created. Never raises."""
    settings = settings or psa_settings
    if not settings.enabled:
        return None
    ref = ticket.get("ref") if isinstance(ticket, Mapping) else None
    try:
        return _sync(conn, ticket, is_demo=is_demo, settings=settings, adapter=adapter)
    except Exception as exc:  # The PSA must never break the pipeline.
        logger.error("psa sync failed provider=%s ref=%s error=%s", settings.provider, ref, type(exc).__name__)
        _record(
            conn,
            ticket,
            "psa_failed",
            {
                "reason": f"PSA sync failed unexpectedly ({type(exc).__name__})",
                "provider": settings.provider,
                "status_code": None,
                "attempts": 0,
                "retryable": False,
            },
        )
        return None


def _sync(
    conn: Any, ticket: Mapping[str, Any], *, is_demo: bool, settings: PsaSettings, adapter: PsaAdapter | None
) -> PsaResult | None:
    provider, label = settings.provider, PROVIDER_LABELS.get(settings.provider, "the PSA")
    if ticket.get("status") == "failed" or _already_synced(conn, ticket):
        return None
    problems = settings.problems()
    if problems:
        reason = f"PSA sync is switched on but these settings are missing: {', '.join(problems)}"
        _record(conn, ticket, "psa_skipped", {"provider": provider, "reason": reason})
        return None
    if is_demo:
        _record(conn, ticket, "psa_skipped", {"provider": provider, "reason": DEMO_REASON})
        return None

    adapter = adapter or factory.get_adapter(settings)
    if adapter is None:
        _record(conn, ticket, "psa_skipped", {"provider": provider, "reason": f"No {label} adapter is available"})
        return None
    try:
        result = adapter.create_ticket(PsaTicket.from_ticket(ticket))
    except PsaError as exc:
        logger.warning(
            "psa sync failed provider=%s ref=%s status=%s attempts=%s retryable=%s",
            provider, ticket.get("ref"), exc.status_code, exc.attempts, exc.retryable,
        )  # fmt: skip
        detail = {
            "reason": exc.message,
            "provider": provider,
            "status_code": exc.status_code,
            "attempts": exc.attempts,
            "retryable": exc.retryable,
        }
        _record(conn, ticket, "psa_failed", detail)
        return None

    shown = result.external_ref or result.external_id
    detail = {
        "description": f"Created in {label} as {shown}",
        "provider": provider,
        "external_id": result.external_id,
        "external_ref": result.external_ref,
        "url": result.url,
        "attempts": result.attempts,
    }
    _record(conn, ticket, "psa_synced", detail)
    logger.info("psa sync done provider=%s ref=%s id=%s", provider, ticket.get("ref"), result.external_id)
    return result


def _already_synced(conn: Any, ticket: Mapping[str, Any]) -> bool:
    return any(event.get("type") == "psa_synced" for event in store.list_events(conn, ticket["id"]))


def _record(conn: Any, ticket: Mapping[str, Any], kind: str, detail: dict[str, Any]) -> None:
    """Best effort. A failed event write is logged, never raised."""
    try:
        store.add_events(conn, ticket["id"], [(kind, detail)])
    except Exception as exc:
        logger.error("psa event write failed type=%s ref=%s error=%s", kind, ticket.get("ref"), type(exc).__name__)
