"""Alert body formats: host detection, Slack and Discord text, and the Teams Adaptive Card."""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fakes import FakeStore

from triageops import alert_formats, alerts, triage
from triageops.alert_formats import (
    TEAMS_PAYLOAD_BUDGET_BYTES,
    TEAMS_PAYLOAD_LIMIT_BYTES,
    AlertFormatSettings,
    build_payload,
    detect_format,
    escape_card_markdown,
    escape_slack,
    payload_size,
    ticket_link,
)
from triageops.config import settings
from triageops.schemas import InboundTicket

TEAMS_CONNECTOR_URL = "https://acme.webhook.office.com/webhookb2/abc@def/IncomingWebhook/123/456"
TEAMS_WORKFLOW_URL = (
    "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke"
    "?api-version=2016-06-01&sig=secret"
)
TEAMS_POWERPLATFORM_URL = (
    "https://default0123abcd.ef.environment.api.powerplatform.com:443/powerautomate/automations/direct"
    "/workflows/abc/triggers/manual/paths/invoke?api-version=1&sig=secret"
)
SLACK_URL = "https://hooks.slack.com/services/T000/B000/XXXX"
DISCORD_URL = "https://discord.com/api/webhooks/123/token"
GENERIC_URL = "https://example.com/hooks/alerts"

BODY = "SECRET BODY TEXT the customer wrote"
REPLY = "SECRET DRAFT REPLY for review"


def ticket(**overrides: Any) -> dict[str, Any]:
    base = {
        "ref": "TKT-1104-0001",
        "customer": "Northwind Dental",
        "subject": "Ransom note on every PC",
        "summary": "Files are encrypted across the practice.",
        "body": BODY,
        "suggested_reply": REPLY,
        "priority": "P1",
        "team": "Security Ops",
        "status": "escalated",
        "channel": "email",
        "sla_due_at": datetime(2026, 11, 4, 10, 0, tzinfo=UTC),
    }
    return {**base, **overrides}


def card_of(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["attachments"][0]["content"]


def card_texts(card: dict[str, Any]) -> list[str]:
    texts = [block["text"] for block in card["body"] if block["type"] == "TextBlock"]
    facts = next(block["facts"] for block in card["body"] if block["type"] == "FactSet")
    return texts + [fact["value"] for fact in facts]


def facts_of(card: dict[str, Any]) -> dict[str, str]:
    facts = next(block["facts"] for block in card["body"] if block["type"] == "FactSet")
    return {fact["title"]: fact["value"] for fact in facts}


def unescaped(text: str, chars: str = "*_~`[]<>&#|{}") -> list[str]:
    """Markdown characters not preceded by a backslash escape."""
    found, index = [], 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] in chars:
            found.append(text[index])
        index += 1
    return found


# Detection ------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (TEAMS_CONNECTOR_URL, "teams"),
        (TEAMS_WORKFLOW_URL, "teams"),
        (TEAMS_POWERPLATFORM_URL, "teams"),
        ("https://outlook.office.com/webhook/abc/IncomingWebhook/def", "teams"),
        (SLACK_URL, "slack"),
        (DISCORD_URL, "discord"),
        ("https://discordapp.com/api/webhooks/123/token", "discord"),
        ("https://discord.com/channels/123", "generic"),
        ("https://outlook.office.com/mail", "generic"),
        (GENERIC_URL, "generic"),
        ("https://evilwebhook.office.com/x", "generic"),
        ("https://webhook.office.com.evil.example/x", "generic"),
        ("https://hooks.slack.com.evil.example/services/x", "generic"),
        ("https://hooks.slack.com@evil.example/services/x", "generic"),
        ("https://HOOKS.SLACK.COM/services/x", "slack"),
        ("not a url", "generic"),
        ("", "generic"),
    ],
)
def test_detect_format_by_host(url: str, expected: str) -> None:
    assert detect_format(url) == expected


@pytest.mark.parametrize("override", ["teams", "slack", "discord", "generic", " Teams "])
def test_valid_override_wins(override: str) -> None:
    assert detect_format(GENERIC_URL, override) == override.strip().lower()


@pytest.mark.parametrize("override", ["auto", "", "msteams", "html", "AUTO"])
def test_invalid_or_auto_override_detects_by_host(override: str) -> None:
    assert detect_format(SLACK_URL, override) == "slack"
    assert detect_format(GENERIC_URL, override) == "generic"


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_FORMAT", " Teams ")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://triageops.vercel.app ")
    assert AlertFormatSettings.from_env() == AlertFormatSettings("teams", "https://triageops.vercel.app")

    monkeypatch.setenv("ALERT_FORMAT", "fancy")
    monkeypatch.delenv("PUBLIC_APP_URL")
    assert AlertFormatSettings.from_env() == AlertFormatSettings("auto", "")


# Plain text formats ---------------------------------------------------------------

def test_generic_body_is_unchanged() -> None:
    message = alerts.format_alert(ticket(), "Ransomware reported")
    payload = build_payload(ticket(), "Ransomware reported", "generic")
    assert payload == {"text": message, "content": message}
    assert list(payload) == ["text", "content"]
    assert json.dumps(payload) == json.dumps({"text": message, "content": message})


def test_slack_body_escapes_control_characters() -> None:
    hostile = ticket(customer="<!channel> & <https://evil.example|click>")
    payload = build_payload(hostile, "P1 ticket", "slack")
    assert list(payload) == ["text"]
    text = payload["text"]
    assert "<" not in text and ">" not in text
    assert "&lt;!channel&gt; &amp; &lt;https://evil.example|click&gt;" in text
    assert escape_slack("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    assert escape_slack("&amp;") == "&amp;amp;", "a literal entity must stay literal"


def test_discord_body_is_capped_and_cannot_ping() -> None:
    payload = build_payload(ticket(summary="x" * 5000, customer="@everyone"), "P1 ticket", "discord")
    assert set(payload) == {"content", "allowed_mentions"}
    assert len(payload["content"]) <= 1900
    assert payload["allowed_mentions"] == {"parse": []}


@pytest.mark.parametrize("fmt", ["generic", "slack", "discord", "teams"])
def test_no_format_leaks_the_body_or_the_reply(fmt: str) -> None:
    payload = build_payload(ticket(), "P1 ticket", fmt, app_url="https://triageops.vercel.app")  # type: ignore[arg-type]
    encoded = json.dumps(payload)
    assert "SECRET BODY" not in encoded
    assert "SECRET DRAFT" not in encoded


# Teams card -----------------------------------------------------------------------

def test_teams_message_shape() -> None:
    payload = build_payload(ticket(), "Ransomware reported", "teams")
    assert payload["type"] == "message"
    assert len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["contentUrl"] is None

    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.4"
    assert card["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
    assert card["msteams"] == {"width": "Full"}
    assert [block["type"] for block in card["body"]] == ["TextBlock", "TextBlock", "FactSet"]
    title, summary = card["body"][0], card["body"][1]
    assert title["text"] == "[P1] TKT-1104-0001 for Northwind Dental"
    assert title["weight"] == "Bolder" and title["wrap"] is True
    assert summary["text"] == "Files are encrypted across the practice."
    assert summary["wrap"] is True
    assert "actions" not in card


def test_teams_facts() -> None:
    card = card_of(build_payload(ticket(), "Ransomware reported", "teams"))
    assert facts_of(card) == {
        "Priority": "P1",
        "Team": "Security Ops",
        "Status": "escalated",
        "SLA due": "04 Nov 10:00 UTC",
        "Channel": "email",
        "Reason": "Ransomware reported",
    }


def test_teams_card_falls_back_to_subject_and_defaults() -> None:
    sparse = {"ref": "TKT-1104-0009", "subject": "Printer offline"}
    card = card_of(build_payload(sparse, "", "teams"))
    assert card["body"][0]["text"] == "[P?] TKT-1104-0009 for an unknown customer"
    assert card["body"][1]["text"] == "Printer offline"
    facts = facts_of(card)
    assert facts["Team"] == "unassigned"
    assert facts["SLA due"] == "not set"
    assert facts["Reason"] == "not given"


@pytest.mark.parametrize(("priority", "colour"), [("P1", "Attention"), ("P2", "Warning"), ("P3", "Default"),
                                                  ("P4", "Default"), (None, "Default"), ("P9", "Default")])  # fmt: skip
def test_title_colour_follows_priority(priority: str | None, colour: str) -> None:
    card = card_of(build_payload(ticket(priority=priority), "reason", "teams"))
    assert card["body"][0]["color"] == colour


HOSTILE = [
    "[click](https://evil.example)",
    "**bold** and _italic_ and ~strike~",
    "<at>everyone</at> <https://evil.example>",
    "- list item",
    "1. numbered",
    "# heading",
    "&#91;click&#93;(https://evil.example)",
    "`code` | pipe {braces}",
    "back\\slash \\[still escaped\\]",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_ticket_text_is_escaped_everywhere(text: str) -> None:
    hostile = ticket(subject=text, summary=text, customer=text, ref=text, team=text, status=text, channel=text)
    card = card_of(build_payload(hostile, text, "teams"))
    for value in card_texts(card):
        assert "](" not in value.replace("\\]", ""), value
        assert "://" not in value, "bare URLs must not autolink"
        # The title template itself writes the literal [P1] prefix.
        assert unescaped(value.removeprefix("[P1] ")) == [], value


def test_markdown_link_renders_inert() -> None:
    card = card_of(build_payload(ticket(summary="[click](https://evil.example)"), "reason", "teams"))
    assert card["body"][1]["text"] == "\\[click\\](https:\\/\\/evil.example)"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("**bold**", "\\*\\*bold\\*\\*"),
        ("- item", "\\- item"),
        ("12. item", "12\\. item"),
        ("3) item", "3\\) item"),
        ("+ item", "\\+ item"),
        ("Smith & Co", "Smith \\& Co"),
        ("line one\nline two\r- three", "line one line two - three"),
        ("zero​width\x00null", "zero width null"),
        ("plain text, P1", "plain text, P1"),
    ],
)
def test_escape_card_markdown(text: str, expected: str) -> None:
    assert escape_card_markdown(text) == expected


def test_escape_card_markdown_truncates_before_escaping() -> None:
    escaped = escape_card_markdown("*" * 1000, 50)
    assert escaped.endswith("...")
    assert escaped.count("\\*") == 47


@pytest.mark.parametrize(
    ("app_url", "expected"),
    [
        ("https://triageops.vercel.app", "https://triageops.vercel.app/ticket/TKT-1104-0001"),
        ("https://triageops.vercel.app/", "https://triageops.vercel.app/ticket/TKT-1104-0001"),
        ("https://example.com/ops/", "https://example.com/ops/ticket/TKT-1104-0001"),
        ("http://triageops.vercel.app", None),
        ("javascript:alert(1)", None),
        ("https://user:pass@example.com", None),
        ("https://", None),
        ("", None),
        (None, None),
    ],
)
def test_open_url_only_for_https_app_url(app_url: str | None, expected: str | None) -> None:
    assert ticket_link(app_url, "TKT-1104-0001") == expected
    card = card_of(build_payload(ticket(), "reason", "teams", app_url=app_url))
    if expected is None:
        assert "actions" not in card
    else:
        assert card["actions"] == [{"type": "Action.OpenUrl", "title": "Open ticket", "url": expected}]


@pytest.mark.parametrize("ref", ["TKT 1/../x", "../admin", "x" * 65, "", None, 42])
def test_open_url_needs_a_well_formed_ref(ref: Any) -> None:
    assert ticket_link("https://ops.example", ref) is None


def test_teams_card_stays_under_the_size_limit() -> None:
    huge = "[x](y)*" * 20_000
    payload = build_payload(
        ticket(summary=huge, subject=huge, customer=huge, ref=huge, team=huge, status=huge, channel=huge),
        huge,
        "teams",
        app_url="https://triageops.vercel.app",
    )
    size = payload_size(payload)
    assert size <= TEAMS_PAYLOAD_BUDGET_BYTES < TEAMS_PAYLOAD_LIMIT_BYTES
    assert len(httpx.Request("POST", GENERIC_URL, json=payload).content) == size


def test_teams_card_shrinks_a_multibyte_summary_to_fit() -> None:
    summary = "\U0001f600" * 1500
    payload = build_payload(ticket(summary=summary), "reason", "teams")
    assert payload_size(payload) <= TEAMS_PAYLOAD_BUDGET_BYTES
    assert card_of(payload)["body"][1]["text"].startswith("\U0001f600")


# send_alert -----------------------------------------------------------------------

class Recorder:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(self.status_code, request=httpx.Request("POST", url))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(alerts.httpx, "post", rec.post)
    monkeypatch.setattr(alert_formats, "alert_format_settings", AlertFormatSettings("auto", ""))
    return rec


def test_send_alert_posts_a_teams_card_and_accepts_202(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder.status_code = 202
    monkeypatch.setattr(settings, "alert_webhook_url", TEAMS_WORKFLOW_URL)
    monkeypatch.setattr(alert_formats, "alert_format_settings",
                        AlertFormatSettings("auto", "https://triageops.vercel.app"))  # fmt: skip

    result = alerts.send_alert(ticket(), "Ransomware reported")

    assert result.status == "sent"
    assert result.as_event() == ("alert_sent", {"reason": "Ransomware reported", "status_code": 202, "format": "teams"})
    sent = recorder.calls[0]["json"]
    assert sent["type"] == "message"
    assert card_of(sent)["actions"][0]["url"] == "https://triageops.vercel.app/ticket/TKT-1104-0001"


def test_send_alert_generic_host_posts_the_original_body(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "alert_webhook_url", GENERIC_URL)
    result = alerts.send_alert(ticket(), "Ransomware reported")
    message = alerts.format_alert(ticket(), "Ransomware reported")
    assert recorder.calls[0]["json"] == {"text": message, "content": message}
    assert result.detail["format"] == "generic"


def test_send_alert_override_forces_teams(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "alert_webhook_url", GENERIC_URL)
    monkeypatch.setattr(alert_formats, "alert_format_settings", AlertFormatSettings("teams", ""))
    result = alerts.send_alert(ticket(), "reason")
    assert recorder.calls[0]["json"]["type"] == "message"
    assert result.detail["format"] == "teams"


def test_send_alert_failure_records_the_format(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder.status_code = 413
    monkeypatch.setattr(settings, "alert_webhook_url", SLACK_URL)
    result = alerts.send_alert(ticket(), "reason")
    assert result.as_event() == ("error", {"stage": "alert", "reason": "alert webhook failed (HTTPStatusError)",
                                           "format": "slack"})  # fmt: skip


def test_send_alert_falls_back_to_generic_if_the_card_cannot_be_built(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(settings, "alert_webhook_url", TEAMS_CONNECTOR_URL)
    monkeypatch.setattr(alert_formats, "build_payload", broken)
    result = alerts.send_alert(ticket(), "reason")
    assert result.status == "sent"
    assert set(recorder.calls[0]["json"]) == {"text", "content"}


def test_pipeline_sends_a_teams_card_for_a_p1(
    fake_store: FakeStore, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "alert_webhook_url", TEAMS_CONNECTOR_URL)
    inbound = InboundTicket(
        channel="email",
        subject="**Ransomware** [click](https://evil.example)",
        body="Every file is encrypted and there is a ransom note on the desktop. SECRET BODY TEXT",
        customer="Northwind Dental",
    )

    bundle = triage.process_ticket(None, inbound)

    assert bundle.ticket["priority"] == "P1"
    alert = next(e for e in bundle.events if e["type"] == "alert_sent")
    assert alert["detail"]["format"] == "teams"
    sent = recorder.calls[0]["json"]
    assert "SECRET BODY" not in json.dumps(sent)
    for value in card_texts(card_of(sent)):
        assert unescaped(value.removeprefix("[P1] ")) == [], value
