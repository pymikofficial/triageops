"""Turn an ``RmmAlert`` into the channel neutral ``InboundTicket`` the pipeline triages.

The subject and body are plain text built from the alert. The pipeline masks
them before storage and fences them as untrusted ticket text in the prompt.
The priority hint and its reason come from our own vocabulary and travel
outside the ticket block.
"""

from datetime import UTC

from ..schemas import InboundTicket
from .models import (
    SEVERITY_LABELS,
    VENDOR_LABELS,
    RmmAlert,
    priority_hint,
    priority_hint_reason,
)

SUBJECT_MAX_CHARS = 200
BODY_MAX_CHARS = 4000
NO_MESSAGE = "The RMM did not include an alert message."
NO_PRIORITY = "Not supplied. The RMM did not supply a priority, so judge it from the alert."


def _device_label(alert: RmmAlert) -> str:
    return alert.device_name or f"device {alert.device_id}"


def build_subject(alert: RmmAlert) -> str:
    """For example ``"Disk Free Space < 10% on ACME-FS01"``, at most 200 characters."""
    device = " ".join(_device_label(alert).split())[:80]
    name = " ".join((alert.condition_name or "RMM alert").split())
    suffix = f" on {device}"
    room = SUBJECT_MAX_CHARS - len(suffix)
    if len(name) > room:
        name = name[: room - 3].rstrip() + "..."
    return f"{name}{suffix}"


def _body_lines(alert: RmmAlert) -> list[str]:
    device = _device_label(alert)
    plain = device in (alert.device_id, f"device {alert.device_id}")
    device_line = device if plain else f"{device} (device id {alert.device_id})"
    if alert.severity == "unknown":
        severity_line = NO_PRIORITY
    else:
        severity_line = SEVERITY_LABELS[alert.severity]
    occurred = (
        alert.occurred_at.astimezone(UTC).strftime("%d %b %Y %H:%M UTC") if alert.occurred_at else "not supplied by the RMM"
    )
    return [
        f"Device: {device_line}",
        f"Customer: {alert.customer or 'not supplied by the RMM'}",
        f"Condition: {alert.condition_name or alert.condition}",
        f"Severity: {severity_line}",
        f"Source: {VENDOR_LABELS[alert.vendor]} alert",
        f"Time: {occurred}",
    ]


def build_body(alert: RmmAlert) -> str:
    """The alert message followed by the alert facts, between 10 and 4000 characters."""
    facts = "\n".join(_body_lines(alert))
    room = BODY_MAX_CHARS - len(facts) - 2
    message = (alert.message or NO_MESSAGE).strip() or NO_MESSAGE
    if len(message) > room:
        message = message[: room - 3].rstrip() + "..."
    return f"{message}\n\n{facts}"[:BODY_MAX_CHARS]


def to_inbound(alert: RmmAlert, *, client_hash: str | None) -> InboundTicket:
    return InboundTicket(
        channel="rmm",
        subject=build_subject(alert),
        body=build_body(alert),
        customer=alert.customer,
        requester=None,
        client_hash=client_hash,
        source_id=alert.source_id,
        source_detail={
            "rmm_key": alert.dedupe_key,
            "vendor": alert.vendor,
            "device": _device_label(alert),
            "condition": alert.condition_name or alert.condition,
            "severity": alert.severity,
            "occurrences": 1,
        },
        priority_hint=priority_hint(alert),
        priority_hint_reason=priority_hint_reason(alert),
    )
