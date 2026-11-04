"""Deterministic triage logic that runs in Python, never in the model.

``apply_business_rules`` runs after every triage, whether it came from Claude
or from the fallback, and has the final say on priority, team, escalation and
SLA. Each rule that fires returns an audit event. ``heuristic_triage`` is the
keyword fallback used when Claude is unavailable, so a ticket is never lost.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .decision import TriageDecision, trim_words
from .security import find_injection_marker
from .taxonomy import DEFAULT_TEAM, SLA_HOURS

INJECTION_REASON = "Possible prompt injection, human review"
P1_REASON = "P1 incident that needs an immediate response"

Event = tuple[str, dict[str, Any]]

# Ransomware, encrypted files or a confirmed breach. A passive verb is required
# between the noun and "encrypted" so a request such as "how do I send an
# encrypted file" is not forced to P1.
_CRITICAL_SECURITY = re.compile(
    r"(?i)\bransom(?:ware)?\b"
    r"|\b(?:files?|documents|drives?|folders?|servers?|data)\b[\w\s,']{0,30}?"
    r"\b(?:have|has|had|are|were|got|been|all)\b[\w\s,']{0,15}?\bencrypted\b"
    r"|\bconfirmed\b[\w\s]{0,20}?\bbreach\b|\bbreach\b[\w\s]{0,20}?\bconfirmed\b"
)


def find_critical_security_trigger(text: str) -> str | None:
    match = _CRITICAL_SECURITY.search(text)
    return match.group(0) if match else None


@dataclass
class RuleOutcome:
    decision: TriageDecision
    status: str
    escalation_reason: str | None
    sla_due_at: datetime
    alert: bool
    events: list[Event] = field(default_factory=list)


def _rule(rule: str, description: str, detail: dict[str, Any] | None = None) -> Event:
    return ("rule_applied", {"rule": rule, "description": description, **(detail or {})})


def apply_business_rules(
    decision: TriageDecision, *, subject: str, body: str, created_at: datetime
) -> RuleOutcome:
    """Apply the business rules in a fixed order and return the final state."""
    final = decision.model_copy()
    text = f"{subject}\n{body}"
    events: list[Event] = []

    trigger = find_critical_security_trigger(text)
    if trigger and (final.priority != "P1" or final.team != "Security Ops"):
        events.append(
            _rule(
                "critical_security_p1",
                "Ransomware, encrypted files or a confirmed breach is always P1 for Security Ops",
                {"matched": trigger, "from": f"{final.priority} {final.team}", "to": "P1 Security Ops"},
            )
        )
        final.priority, final.team = "P1", "Security Ops"

    if final.category == "Security" and final.priority in ("P3", "P4"):
        events.append(
            _rule("security_minimum_p2", "Security tickets are never below P2", {"from": final.priority, "to": "P2"})
        )
        final.priority = "P2"

    marker = find_injection_marker(text)
    if marker and not final.injection_suspected:
        events.append(
            _rule("injection_markers", "Instruction override wording was found in the ticket", {"matched": marker})
        )
        final.injection_suspected = True

    reasons: list[str] = []
    if final.injection_suspected:
        reasons.append(INJECTION_REASON)
        events.append(
            _rule("injection_human_review", "A person checks suspected prompt injection before any reply is sent")
        )
        events.append(("human_review", {"reason": INJECTION_REASON}))

    alert = final.priority == "P1" or final.escalate
    if alert:
        reasons.append(final.escalation_reason or P1_REASON)
        events.append(
            _rule(
                "escalate",
                "P1 tickets and tickets the model escalates go to a lead and raise an alert",
                {"trigger": "priority_p1" if final.priority == "P1" else "model_escalation"},
            )
        )

    escalation_reason = ". ".join(dict.fromkeys(reasons)) or None
    status = "escalated" if escalation_reason else "triaged"
    if escalation_reason:
        events.append(("escalated", {"reason": escalation_reason}))

    hours = SLA_HOURS[final.priority]
    sla_due_at = created_at + timedelta(hours=hours)
    events.append(
        _rule(
            "sla",
            f"{final.priority} has a {hours} hour SLA from the time the ticket was received",
            {"priority": final.priority, "hours": hours, "sla_due_at": sla_due_at.isoformat()},
        )
    )

    return RuleOutcome(
        decision=final,
        status=status,
        escalation_reason=escalation_reason,
        sla_due_at=sla_due_at,
        alert=alert,
        events=events,
    )


# Keyword fallback ------------------------------------------------------------------

# Checked in order and the first match wins, so more specific categories come first.
_CATEGORY_PATTERNS = (
    (
        "Security",
        r"ransom|encrypted|phish|malware|virus|hacked|breach|suspicious (?:e-?mail|link|sign.?in|login)"
        r"|clicked (?:on )?(?:a|the) link|fake (?:login|page|docusign)",
    ),
    ("Billing & Account", r"invoice|billing|\bbill\b|charged|refund|renewal|payment|\bquote\b"),
    ("Backup & Recovery", r"backup|restore|recover|deleted (?:a |the |some )?(?:file|folder|email)"),
    (
        "Access & Identity",
        r"\bmfa\b|authenticator|\b2fa\b|two.factor|locked out|password|log ?in|sign ?in"
        r"|account (?:is )?locked|new starter|leaver",
    ),
    (
        "Email & M365",
        r"outlook|mailbox|e-?mail|\bteams\b|onedrive|sharepoint|\bm365\b|office 365|microsoft 365"
        r"|calendar|out of office",
    ),
    (
        "Hardware",
        r"printer|laptop|monitor|screen|keyboard|mouse|dock|headset|\bdisk\b|hard drive|battery|scanner|\bslow\b",
    ),
    ("Network", r"\bvpn\b|wi-?fi|wireless|network|internet|broadband|router|firewall|\bswitch\b|connection"),
    ("Software", r"install|software|application|\bapp\b|update|excel|\bword\b|adobe|crash"),
)
_CATEGORY_KEYWORDS = tuple((category, re.compile(pattern, re.IGNORECASE)) for category, pattern in _CATEGORY_PATTERNS)

_P1_SIGNALS = re.compile(
    r"(?i)ransom|breach|(?:whole|entire) (?:\w+ )?(?:office|site|depot|branch|warehouse|practice|shop|store|company"
    r"|team|building)|(?:all|every) (?:users|staff|machines|computers)|(?:nobody|no one)\b[^.]{0,30}?\bcan\b"
    r"|everyone (?:is|has)|completely down|outage|server (?:is )?down"
)
_P2_SIGNALS = re.compile(
    r"(?i)urgent|asap|cannot work|locked out|blocked|phish|clicked|\bdown\b|deadline|meeting at|by \d{1,2}(?:am|pm)"
)
_P4_SIGNALS = re.compile(
    r"(?i)how (?:do|can|would) i|how to|is it possible|when you (?:get|have) a (?:chance|moment)|no rush"
    r"|question about|invoice|billing|could you (?:set up|add|show|remind)"
)

_ANGRY = re.compile(r"(?i)unacceptable|ridiculous|furious|disgrace|appalling|useless|fed up|sick of|cancel (?:our|the)|!!")
_FRUSTRATED = re.compile(
    r"(?i)\bagain\b|\bstill\b|third time|second time|frustrat|annoying|keeps? (?:dropping|happening)|not happy"
)
_POSITIVE = re.compile(r"(?i)great job|brilliant|fantastic|really appreciate|thank you so much")

_NEXT_STEP_BY_CATEGORY = {
    "Security": "Isolate affected devices and accounts, preserve logs and follow the security incident runbook.",
    "Network": "Check monitoring for the site and confirm how many users are affected.",
    "Hardware": "Check the device status remotely and arrange a swap or site visit if needed.",
    "Software": "Confirm the application version and reproduce the issue on the user device.",
    "Access & Identity": "Verify the requester identity by a known phone number before any reset.",
    "Email & M365": "Check the Microsoft 365 service health and the user mailbox settings.",
    "Backup & Recovery": "Locate the latest good restore point and confirm the restore location.",
    "Billing & Account": "Pass the query to the account manager with the invoice reference.",
    "Other": "Clarify the request with the requester.",
}

# gte-small scores even unrelated text quite high, so only close matches are cited.
FALLBACK_KB_MIN_SIMILARITY = 0.8


def _category(text: str) -> str:
    return next((category for category, pattern in _CATEGORY_KEYWORDS if pattern.search(text)), "Other")


def _priority(text: str) -> str:
    if find_critical_security_trigger(text) or _P1_SIGNALS.search(text):
        return "P1"
    if _P2_SIGNALS.search(text):
        return "P2"
    if _P4_SIGNALS.search(text):
        return "P4"
    return "P3"


def _sentiment(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    shouting = len(letters) > 40 and sum(c.isupper() for c in letters) / len(letters) > 0.5
    if shouting or _ANGRY.search(text):
        return "angry"
    if _FRUSTRATED.search(text):
        return "frustrated"
    if _POSITIVE.search(text):
        return "positive"
    return "neutral"


def _fallback_reply(team: str, category: str, kb_title: str | None) -> str:
    paragraphs = [
        "Hello,",
        "Thank you for contacting the service desk. We have logged your request and passed it to our "
        f"{team} team, who will be in touch in line with your service agreement.",
    ]
    if category == "Security":
        paragraphs.append(
            "If you think a device is infected, disconnect it from the network now but do not switch it off, "
            "and do not reply to or forward any suspicious messages."
        )
    if kb_title:
        paragraphs.append(f'In the meantime, our guide "{kb_title}" may help.')
    paragraphs.append("Kind regards,\nTriageOps Service Desk")
    return "\n\n".join(paragraphs)


def heuristic_triage(subject: str, body: str, articles: list[Any]) -> TriageDecision:
    """Keyword triage for when Claude is unavailable. ``articles`` are ``rag.KbMatch`` items."""
    text = f"{subject}\n{body}"
    category = _category(text)
    team = DEFAULT_TEAM[category]
    best = articles[0] if articles and articles[0].similarity >= FALLBACK_KB_MIN_SIMILARITY else None
    return TriageDecision(
        category=category,
        priority=_priority(text),
        sentiment=_sentiment(text),
        team=team,
        summary=trim_words(" ".join(subject.split()), 25),
        suggested_reply=_fallback_reply(team, category, best.title if best else None),
        next_steps=[
            "Confirm the issue and its business impact with the requester.",
            _NEXT_STEP_BY_CATEGORY[category],
            "Update the ticket with findings and the next contact time.",
        ],
        kb_refs=[best.slug] if best else [],
        confidence=0.3,
        injection_suspected=find_injection_marker(text) is not None,
    )
