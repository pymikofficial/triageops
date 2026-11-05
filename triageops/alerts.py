"""Outbound alerts for P1, escalated and SLA breached tickets.

The body format follows the webhook host (see ``alert_formats``): Slack and
Discord get plain text, Teams gets an Adaptive Card, and any other host gets
``{"text": ..., "content": ...}`` as before. Alerts carry the masked ticket
metadata only, never the ticket body.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from . import alert_formats
from .config import settings

logger = logging.getLogger(__name__)

ALERT_TIMEOUT_SECONDS = 5.0
# Discord rejects content longer than 2000 characters.
MAX_ALERT_CHARS = 1900

_EVENT_FOR_STATUS = {"sent": "alert_sent", "skipped": "alert_skipped", "failed": "error"}


@dataclass(frozen=True)
class AlertResult:
    status: Literal["sent", "skipped", "failed"]
    detail: dict[str, Any] = field(default_factory=dict)

    def as_event(self) -> tuple[str, dict[str, Any]]:
        return _EVENT_FOR_STATUS[self.status], self.detail


def _format_due(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%d %b %H:%M UTC")
    return str(value or "not set")


def format_alert(ticket: dict[str, Any], reason: str) -> str:
    lines = [
        f"[{ticket.get('priority') or 'P?'}] {ticket.get('ref')} for {ticket.get('customer') or 'an unknown customer'}",
        ticket.get("summary") or ticket.get("subject") or "",
        f"Team {ticket.get('team') or 'unassigned'}, SLA due {_format_due(ticket.get('sla_due_at'))}",
        f"Reason {reason}",
    ]
    return "\n".join(line for line in lines if line)[:MAX_ALERT_CHARS]


def send_alert(ticket: dict[str, Any], reason: str) -> AlertResult:
    """Post an alert if ``ALERT_WEBHOOK_URL`` is set. Never raises."""
    url = settings.alert_webhook_url
    if not url:
        return AlertResult("skipped", {"reason": "ALERT_WEBHOOK_URL is not configured"})
    if not url.startswith("https://"):
        return AlertResult("failed", {"stage": "alert", "reason": "ALERT_WEBHOOK_URL must use https"})

    fmt = alert_formats.detect_format(url, alert_formats.alert_format_settings.format)
    payload = _payload(ticket, reason, fmt)
    try:
        response = httpx.post(url, json=payload, timeout=ALERT_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("alert failed ref=%s format=%s error=%s", ticket.get("ref"), fmt, type(exc).__name__)
        reason_text = f"alert webhook failed ({type(exc).__name__})"
        return AlertResult("failed", {"stage": "alert", "reason": reason_text, "format": fmt})
    return AlertResult("sent", {"reason": reason, "status_code": response.status_code, "format": fmt})


def _payload(ticket: dict[str, Any], reason: str, fmt: alert_formats.AlertFormat) -> dict[str, Any]:
    """The body for ``fmt``, or the generic body if building it fails, so an alert still goes out."""
    try:
        return alert_formats.build_payload(ticket, reason, fmt, app_url=alert_formats.alert_format_settings.public_app_url)
    except Exception as exc:
        logger.warning("alert payload failed ref=%s format=%s error=%s", ticket.get("ref"), fmt, type(exc).__name__)
        message = format_alert(ticket, reason)
        return {"text": message, "content": message}
