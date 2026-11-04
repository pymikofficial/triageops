"""Webhook body formats for outbound alerts: generic, Slack, Discord and Teams.

The format is chosen from the webhook host unless ``ALERT_FORMAT`` names one.
Unknown hosts keep the original generic body ``{"text": ..., "content": ...}``.
Alerts carry masked ticket metadata only, never the ticket body or the
suggested reply.

Facts this module relies on, checked in November 2026:

* Teams incoming webhooks (Office 365 connectors) and the Teams Workflows
  trigger "When a Teams webhook request is received" both accept a message
  body ``{"type": "message", "attachments": [{"contentType":
  "application/vnd.microsoft.card.adaptive", "contentUrl": null, "content":
  {...}}]}``. See
  https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook
  and https://learn.microsoft.com/en-us/connectors/teams/
* Teams messages are limited to about 28 KB including every element, larger
  posts fail with "Request Entity too large". See
  https://learn.microsoft.com/en-us/connectors/teams/
* Workflows answers 202 Accepted once the run is queued, connectors answer
  200. Any 2xx counts as sent. See
  https://www.svix.com/resources/guides/how-to-get-microsoft-teams-webhook-url/
* Adaptive Card version 1.4 is the widely supported choice for Workflows. See
  https://argo-cd.readthedocs.io/en/stable/operator-manual/notifications/services/teams-workflows/
* Workflows URLs moved from ``*.logic.azure.com`` to
  ``*.environment.api.powerplatform.com`` during 2025, so both are detected.
  Legacy connector URLs use ``*.webhook.office.com``. See
  https://blog-en.topedia.com/2025/10/http-and-teams-webhook-trigger-urls-must-be-updated-in-power-automate-and-copilot-studio-agent-flows/
* Teams renders a markdown subset (bold, italic, lists and links) in
  TextBlock text and in Fact titles and values, so ticket derived text is
  escaped. See
  https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-format
* Slack message text must have ``&``, ``<`` and ``>`` replaced with ``&amp;``,
  ``&lt;`` and ``&gt;``, and nothing else is escaped. See
  https://api.slack.com/reference/surfaces/formatting
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, get_args
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

AlertFormat = Literal["generic", "slack", "discord", "teams"]
ALERT_FORMATS: tuple[str, ...] = get_args(AlertFormat)

ADAPTIVE_CARD_VERSION = "1.4"
ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
# Teams rejects messages over about 28 KB. Stay well inside it.
TEAMS_PAYLOAD_LIMIT_BYTES = 28 * 1024
TEAMS_PAYLOAD_BUDGET_BYTES = 24 * 1024
# Discord rejects content longer than 2000 characters.
DISCORD_MAX_CHARS = 1900

SUMMARY_MAX_CHARS = 1500
TITLE_PART_MAX_CHARS = 160
FACT_MAX_CHARS = 300

_TEAMS_HOST_SUFFIXES = (".webhook.office.com", ".logic.azure.com", ".powerplatform.com")
_LEGACY_TEAMS_HOSTS = frozenset({"outlook.office.com", "outlook.office365.com"})
_DISCORD_HOSTS = frozenset({"discord.com", "discordapp.com"})
_PRIORITY_COLOURS = {"P1": "Attention", "P2": "Warning"}
_PRIORITIES = frozenset({"P1", "P2", "P3", "P4"})
# Ticket refs look like TKT-1104-0001. Anything else gets no link.
_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁤﻿]")
# CommonMark backslash escapes. Escaping [ ] stops links, * _ ~ stop emphasis,
# < > stop autolinks and HTML, & stops entities from rebuilding a link.
_CARD_MARKDOWN = re.compile(r"([\\`*_~\[\]<>#|{}&])")
# A line that starts with these becomes a list or a heading.
_LEADING_BLOCK_MARKER = re.compile(r"^(\d+)([.)])|^([-+=])")


@dataclass(frozen=True)
class AlertFormatSettings:
    format: str
    public_app_url: str

    @classmethod
    def from_env(cls) -> "AlertFormatSettings":
        raw = os.environ.get("ALERT_FORMAT", "").strip().lower() or "auto"
        if raw != "auto" and raw not in ALERT_FORMATS:
            logger.warning("ALERT_FORMAT %r is not recognised, detecting the format from the URL", raw[:20])
            raw = "auto"
        return cls(format=raw, public_app_url=os.environ.get("PUBLIC_APP_URL", "").strip())


alert_format_settings = AlertFormatSettings.from_env()


# Detection ------------------------------------------------------------------------

def detect_format(url: str, override: str = "auto") -> AlertFormat:
    """Return ``override`` when it names a format, otherwise detect by host."""
    chosen = (override or "").strip().lower()
    if chosen in ALERT_FORMATS:
        return chosen  # type: ignore[return-value]
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return "generic"
    path = parts.path or ""
    if host.endswith(_TEAMS_HOST_SUFFIXES) or (host in _LEGACY_TEAMS_HOSTS and path.startswith("/webhook")):
        return "teams"
    if host == "hooks.slack.com":
        return "slack"
    if host in _DISCORD_HOSTS and path.startswith("/api/webhooks/"):
        return "discord"
    return "generic"


# Escaping -------------------------------------------------------------------------

def _clean(value: Any, limit: int) -> str:
    """One line of plain text: control characters removed, whitespace collapsed, capped."""
    text = " ".join(_CONTROL_CHARS.sub(" ", str(value or "")).split())
    if len(text) > limit:
        text = text[: max(limit - 3, 0)].rstrip() + "..."
    return text


def escape_slack(text: str) -> str:
    """Escape the three characters Slack treats as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_card_markdown(value: Any, limit: int = FACT_MAX_CHARS) -> str:
    """Make ticket derived text render literally in an Adaptive Card TextBlock or Fact."""
    text = _CARD_MARKDOWN.sub(r"\\\1", _clean(value, limit))
    text = _LEADING_BLOCK_MARKER.sub(lambda m: f"{m[1]}\\{m[2]}" if m[1] else f"\\{m[3]}", text)
    # Break bare URLs so Teams does not turn them into links.
    return text.replace("://", ":\\/\\/")


# Payloads -------------------------------------------------------------------------

def ticket_link(app_url: str | None, ref: Any) -> str | None:
    """The dashboard link for a ticket, only for an https base URL."""
    if not app_url or not isinstance(ref, str) or not _REF_PATTERN.fullmatch(ref):
        return None
    try:
        parts = urlsplit(app_url.strip())
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    base = f"https://{parts.netloc}{parts.path.rstrip('/')}"
    return f"{base}/ticket/{quote(ref, safe='')}"


def _priority(ticket: dict[str, Any]) -> str:
    value = str(ticket.get("priority") or "").upper()
    return value if value in _PRIORITIES else "P?"


def _title_block(ticket: dict[str, Any]) -> dict[str, Any]:
    priority = _priority(ticket)
    ref = escape_card_markdown(ticket.get("ref") or "an unnamed ticket", TITLE_PART_MAX_CHARS)
    customer = escape_card_markdown(ticket.get("customer") or "an unknown customer", TITLE_PART_MAX_CHARS)
    return {
        "type": "TextBlock",
        "text": f"[{priority}] {ref} for {customer}",
        "size": "Medium",
        "weight": "Bolder",
        "color": _PRIORITY_COLOURS.get(priority, "Default"),
        "wrap": True,
    }


def _facts(ticket: dict[str, Any], reason: str) -> list[dict[str, str]]:
    from .alerts import _format_due

    rows = [
        ("Priority", _priority(ticket)),
        ("Team", ticket.get("team") or "unassigned"),
        ("Status", ticket.get("status") or "unknown"),
        ("SLA due", _format_due(ticket.get("sla_due_at"))),
        ("Channel", ticket.get("channel") or "unknown"),
        ("Reason", reason or "not given"),
    ]
    return [{"title": title, "value": escape_card_markdown(value)} for title, value in rows]


def build_teams_card(ticket: dict[str, Any], reason: str, *, app_url: str | None = None,
                     summary_chars: int = SUMMARY_MAX_CHARS) -> dict[str, Any]:  # fmt: skip
    """An Adaptive Card with the title, summary, key facts and an optional link."""
    summary = escape_card_markdown(ticket.get("summary") or ticket.get("subject") or "No summary yet", summary_chars)
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": ADAPTIVE_CARD_VERSION,
        "msteams": {"width": "Full"},
        "body": [
            _title_block(ticket),
            {"type": "TextBlock", "text": summary, "wrap": True},
            {"type": "FactSet", "facts": _facts(ticket, reason)},
        ],
    }
    link = ticket_link(app_url, ticket.get("ref"))
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open ticket", "url": link}]
    return card


def _teams_message(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "attachments": [{"contentType": ADAPTIVE_CARD_CONTENT_TYPE, "contentUrl": None, "content": card}],
    }


def payload_size(payload: dict[str, Any]) -> int:
    """Encoded size in bytes, as httpx sends it."""
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def build_teams_payload(ticket: dict[str, Any], reason: str, *, app_url: str | None = None) -> dict[str, Any]:
    """The Teams message, with the summary shortened until it fits the size budget."""
    summary_chars = SUMMARY_MAX_CHARS
    while True:
        payload = _teams_message(build_teams_card(ticket, reason, app_url=app_url, summary_chars=summary_chars))
        if payload_size(payload) <= TEAMS_PAYLOAD_BUDGET_BYTES or summary_chars <= 40:
            return payload
        summary_chars //= 2


def build_payload(ticket: dict[str, Any], reason: str, fmt: AlertFormat, *, app_url: str | None = None) -> dict[str, Any]:
    """The JSON body for one alert in the chosen format."""
    if fmt == "teams":
        return build_teams_payload(ticket, reason, app_url=app_url)

    from .alerts import format_alert

    message = format_alert(ticket, reason)
    if fmt == "slack":
        return {"text": escape_slack(message)}
    if fmt == "discord":
        return {"content": message[:DISCORD_MAX_CHARS], "allowed_mentions": {"parse": []}}
    return {"text": message, "content": message}
