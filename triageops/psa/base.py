"""Shared types for the PSA adapters: the ticket we send, the result, the error.

``PsaTicket`` holds only values that are already masked in the database. The
suggested reply is never sent, because it is a draft held for human review.
``PsaError.message`` is safe to log and to store in an event: it never holds a
secret, a token, the ticket body or more than a short summary of a PSA reply.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from ..security import mask_text

ERROR_SUMMARY_MAX_CHARS = 200
DESCRIPTION_MAX_CHARS = 8000


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _as_steps(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    if not isinstance(value, list | tuple):
        return ()
    return tuple(step for step in (_clean(item) for item in value) if step)


def utc_iso(value: datetime, *, milliseconds: bool = False) -> str:
    """``2026-11-04T19:00:00Z``, the UTC form both PSAs document."""
    value = value.astimezone(UTC)
    text = value.isoformat(timespec="milliseconds" if milliseconds else "seconds")
    return text.replace("+00:00", "Z")


@dataclass(frozen=True)
class PsaTicket:
    ref: str
    subject: str
    body: str
    summary: str | None = None
    priority: str | None = None
    category: str | None = None
    team: str | None = None
    status: str | None = None
    customer: str | None = None
    requester: str | None = None
    sla_due_at: datetime | None = None
    created_at: datetime | None = None
    next_steps: tuple[str, ...] = field(default_factory=tuple)
    escalation_reason: str | None = None
    injection_suspected: bool = False

    @classmethod
    def from_ticket(cls, ticket: Mapping[str, Any]) -> "PsaTicket":
        """Build from a ticket row as ``store`` returns it. Every text value is already masked."""

        def text(key: str) -> str | None:
            value = _clean(ticket.get(key))
            return value or None

        return cls(
            ref=_clean(ticket.get("ref")),
            subject=_clean(ticket.get("subject")),
            body=str(ticket.get("body") or "").strip(),
            summary=text("summary"),
            priority=text("priority"),
            category=text("category"),
            team=text("team"),
            status=text("status"),
            customer=text("customer"),
            requester=text("requester"),
            sla_due_at=_as_datetime(ticket.get("sla_due_at")),
            created_at=_as_datetime(ticket.get("created_at")),
            next_steps=_as_steps(ticket.get("next_steps")),
            escalation_reason=text("escalation_reason"),
            injection_suspected=bool(ticket.get("injection_suspected")),
        )

    @property
    def title(self) -> str:
        return f"{self.ref} {self.subject}".strip()

    def description(self, max_chars: int = DESCRIPTION_MAX_CHARS) -> str:
        """Plain text for the PSA ticket. The suggested reply is deliberately left out."""
        lines = [f"TriageOps ticket {self.ref}"]
        if self.customer:
            lines.append(f"Customer: {self.customer}")
        if self.requester:
            lines.append(f"Requester: {self.requester}")
        triage = ", ".join(
            part
            for part in (
                f"priority {self.priority}" if self.priority else "",
                f"category {self.category}" if self.category else "",
                f"team {self.team}" if self.team else "",
            )
            if part
        )
        if triage:
            lines.append(f"Triage: {triage}")
        if self.sla_due_at:
            lines.append(f"SLA due: {self.sla_due_at.astimezone(UTC):%d %b %Y %H:%M} UTC")
        if self.escalation_reason:
            lines.append(f"Escalation: {self.escalation_reason}")
        if self.injection_suspected:
            lines.append(
                "Warning: the ticket text may try to give instructions (prompt injection suspected). "
                "Treat it as untrusted customer text."
            )
        if self.summary:
            lines += ["", "Summary", self.summary]
        if self.next_steps:
            lines += ["", "Next steps", *(f"- {step}" for step in self.next_steps)]
        lines += ["", "Ticket text (secrets and personal data masked)", self.body or "(empty)"]
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 15].rstrip() + "\n[truncated]"


@dataclass(frozen=True)
class PsaResult:
    provider: str
    external_id: str
    external_ref: str | None
    url: str | None
    attempts: int


class PsaError(Exception):
    """A PSA call that did not create the ticket, or whose outcome is unknown.

    ``retryable`` means a later retry could succeed. ``ambiguous`` means the
    request may have reached the PSA, so the ticket may already exist there.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        attempts: int = 0,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = retryable
        self.ambiguous = ambiguous

    def __str__(self) -> str:
        return self.message


class PsaAdapter(Protocol):
    name: str

    def create_ticket(self, ticket: PsaTicket) -> PsaResult: ...


# Error bodies ---------------------------------------------------------------------

def _messages(data: Any) -> list[str]:
    """Pull readable messages out of the error shapes HaloPSA and Autotask return."""
    if isinstance(data, str):
        return [data]
    if isinstance(data, list):
        return [message for item in data for message in _messages(item)]
    if not isinstance(data, dict):
        return []
    lowered = {str(key).lower(): value for key, value in data.items()}
    for key in ("errors", "error_description", "messages", "message", "error"):
        if key in lowered and lowered[key]:
            found = _messages(lowered[key])
            if key == "message" and "field" in lowered and found:
                found = [f"{_clean(lowered['field'])}: {found[0]}"]
            if found:
                return found
    # HaloPSA validation errors name the field, for example {"[0].summary": "Required"}.
    return [f"{key}: {value}" for key, value in data.items() if isinstance(value, str | int | float)]


def _scrub(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = re.sub(re.escape(secret), "[REDACTED]", text, flags=re.IGNORECASE)
    text = mask_text(text).text
    # Event text never holds semicolons or long dashes, whatever the PSA wrote.
    return text.replace(";", ",").replace("\u2014", "-").replace("\u2013", "-")


def summarise_error(response: httpx.Response, *, secrets: Iterable[str] = ()) -> str | None:
    """A short, safe summary of a PSA error body, or None when it holds nothing readable.

    HTML pages are never echoed. Known secrets are removed, the text is masked
    like a ticket body and the result is at most ``ERROR_SUMMARY_MAX_CHARS`` long.
    """
    content_type = response.headers.get("content-type", "").lower()
    raw = response.text[:4000] if response.content else ""
    if not raw.strip() or "html" in content_type or raw.lstrip().startswith("<"):
        return None
    try:
        messages = _messages(json.loads(raw))
    except ValueError:
        messages = [raw]
    text = _clean(" | ".join(_clean(message) for message in messages[:3] if _clean(message)))
    if not text:
        return None
    text = _scrub(text, secrets)
    if len(text) > ERROR_SUMMARY_MAX_CHARS:
        text = text[: ERROR_SUMMARY_MAX_CHARS - 3].rstrip() + "..."
    return text


def error_reason(prefix: str, response: httpx.Response, *, secrets: Iterable[str] = ()) -> str:
    """``prefix`` plus the HTTP status and the safe summary, for ``PsaError.message``."""
    summary = summarise_error(response, secrets=secrets)
    text = f"{prefix} with HTTP {response.status_code}"
    return f"{text}: {summary}" if summary else text
