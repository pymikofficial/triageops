"""Adapters that turn each channel's payload into a channel neutral ``InboundTicket``.

Provider payloads are parsed defensively. Anything unexpected is skipped
rather than raised, because providers retry failed deliveries.
"""

from typing import Any

from .schemas import InboundTicket, PostmarkInboundIn, TicketIn

SUBJECT_MAX_CHARS = 200
BODY_MAX_CHARS = 4000
WHATSAPP_SUBJECT_CHARS = 60


def from_form(
    payload: TicketIn,
    *,
    channel: str,
    client_hash: str | None,
    is_demo: bool,
    source_id: str | None = None,
) -> InboundTicket:
    return InboundTicket(
        channel=channel,
        subject=payload.subject,
        body=payload.body,
        customer=payload.customer,
        requester=payload.requester,
        client_hash=client_hash,
        is_demo=is_demo,
        source_id=source_id,
    )


def from_postmark(payload: PostmarkInboundIn, *, client_hash: str | None) -> InboundTicket:
    subject = " ".join(payload.subject.split())[:SUBJECT_MAX_CHARS] or "Email with no subject"
    return InboundTicket(
        channel="email",
        subject=subject,
        body=payload.text_body.strip()[:BODY_MAX_CHARS] or subject,
        customer=payload.mailbox_hash or None,
        requester=payload.from_address or None,
        requester_name=payload.from_name or None,
        client_hash=client_hash,
        source_id=f"postmark:{payload.message_id}" if payload.message_id else None,
    )


def _field(container: Any, key: str) -> Any:
    return container.get(key) if isinstance(container, dict) else None


def _items(container: Any, key: str) -> list[dict[str, Any]]:
    value = _field(container, key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def from_whatsapp(payload: Any, *, client_hash: str | None) -> list[InboundTicket]:
    """One ticket per text message in a Meta WhatsApp Cloud API webhook payload."""
    tickets: list[InboundTicket] = []
    for entry in _items(payload, "entry"):
        for change in _items(entry, "changes"):
            value = change.get("value")
            names = {contact.get("wa_id"): _field(contact.get("profile"), "name") for contact in _items(value, "contacts")}
            for message in _items(value, "messages"):
                text = str(_field(message.get("text"), "body") or "").strip() if message.get("type") == "text" else ""
                if not text:
                    continue
                sender = str(message.get("from") or "")
                tickets.append(
                    InboundTicket(
                        channel="whatsapp",
                        subject=" ".join(text.split())[:WHATSAPP_SUBJECT_CHARS],
                        body=text[:BODY_MAX_CHARS],
                        requester=sender or None,
                        requester_name=names.get(sender),
                        client_hash=client_hash,
                        source_id=f"whatsapp:{message['id']}" if message.get("id") else None,
                    )
                )
    return tickets
