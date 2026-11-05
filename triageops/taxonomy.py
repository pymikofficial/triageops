"""Ticket taxonomy shared by the prompt, the business rules, the API and the tools.

Each vocabulary is declared once as a ``Literal`` type for validation, with a
tuple of its values derived from it for prompts, JSON schemas and SQL filters.
"""

from typing import Final, Literal, get_args

Category = Literal[
    "Network",
    "Hardware",
    "Software",
    "Access & Identity",
    "Security",
    "Email & M365",
    "Backup & Recovery",
    "Billing & Account",
    "Other",
]
Priority = Literal["P1", "P2", "P3", "P4"]
Sentiment = Literal["positive", "neutral", "frustrated", "angry"]
Team = Literal[
    "Service Desk L1",
    "Infrastructure L2",
    "Security Ops",
    "Cloud & M365",
    "Account Management",
]
Channel = Literal["email", "whatsapp", "webhook", "portal", "rmm"]
Status = Literal["new", "triaged", "escalated", "resolved", "failed"]

CATEGORIES: Final[tuple[str, ...]] = get_args(Category)
PRIORITIES: Final[tuple[str, ...]] = get_args(Priority)
SENTIMENTS: Final[tuple[str, ...]] = get_args(Sentiment)
TEAMS: Final[tuple[str, ...]] = get_args(Team)
CHANNELS: Final[tuple[str, ...]] = get_args(Channel)
STATUSES: Final[tuple[str, ...]] = get_args(Status)

OPEN_STATUSES: Final[tuple[str, ...]] = ("new", "triaged", "escalated")

SLA_HOURS: Final[dict[str, int]] = {"P1": 1, "P2": 4, "P3": 24, "P4": 72}

PRIORITY_MEANING: Final[dict[str, str]] = {
    "P1": "critical: outage for many users, security incident, ransomware or data loss",
    "P2": "high: one business critical user blocked, or a degraded service",
    "P3": "normal: a fault for one user, or a fault with a workaround",
    "P4": "low: how-to question, information or a planned request",
}

# Used when Claude returns a team we do not recognise, and by the rules fallback.
DEFAULT_TEAM: Final[dict[str, str]] = {
    "Network": "Infrastructure L2",
    "Hardware": "Service Desk L1",
    "Software": "Service Desk L1",
    "Access & Identity": "Service Desk L1",
    "Security": "Security Ops",
    "Email & M365": "Cloud & M365",
    "Backup & Recovery": "Infrastructure L2",
    "Billing & Account": "Account Management",
    "Other": "Service Desk L1",
}
