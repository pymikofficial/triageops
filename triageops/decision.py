"""The triage decision and the validation that repairs a model's tool input.

Claude is forced to call ``record_triage``, but tool input is still model
output. ``TriageDecision`` accepts anything, repairs what it can (aliases,
casing, ranges, lengths) and records each repair, so a slightly wrong answer
never becomes a failed ticket and every correction is visible in the audit
trail. Pass ``context={"repairs": []}`` to ``model_validate`` to collect them.
"""

import math
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from .taxonomy import (
    CATEGORIES,
    DEFAULT_TEAM,
    TEAMS,
    Category,
    Priority,
    Sentiment,
    Team,
)

SUMMARY_MAX_WORDS = 25
REPLY_MAX_WORDS = 120
NEXT_STEPS_MAX = 5
NEXT_STEP_MAX_CHARS = 160
REASON_MAX_CHARS = 200
DEFAULT_NEXT_STEP = "Review the ticket and confirm the impact with the requester."

_PRIORITY_ALIASES = {
    "p1": "P1", "1": "P1", "critical": "P1", "urgent": "P1", "sev1": "P1",
    "p2": "P2", "2": "P2", "high": "P2", "sev2": "P2",
    "p3": "P3", "3": "P3", "normal": "P3", "medium": "P3", "sev3": "P3",
    "p4": "P4", "4": "P4", "low": "P4", "sev4": "P4",
}  # fmt: skip
_SENTIMENT_ALIASES = {
    "positive": "positive", "happy": "positive",
    "neutral": "neutral", "mixed": "neutral",
    "frustrated": "frustrated", "negative": "frustrated", "annoyed": "frustrated",
    "angry": "angry", "furious": "angry", "hostile": "angry",
}  # fmt: skip


def _note(info: ValidationInfo, message: str) -> None:
    if isinstance(info.context, dict):
        info.context.setdefault("repairs", []).append(message)


def _match_choice(value: Any, choices: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    folded = value.strip().casefold()
    return next((choice for choice in choices if choice.casefold() == folded), None)


def trim_words(text: str, limit: int) -> str:
    """Cut text after ``limit`` words, keeping the original spacing and line breaks."""
    words = list(re.finditer(r"\S+", text))
    if len(words) <= limit:
        return text.strip()
    return text[: words[limit - 1].end()].strip()


def trim_reply(text: str, limit: int = REPLY_MAX_WORDS) -> str:
    """Trim to ``limit`` words, then back to the last full sentence if one is close."""
    trimmed = trim_words(text, limit)
    if trimmed == text.strip():
        return trimmed
    last_stop = max(trimmed.rfind(mark) for mark in ".!?")
    return trimmed[: last_stop + 1] if last_stop > len(trimmed) // 2 else trimmed


def plain_punctuation(text: str) -> str:
    """Swap em dashes, spaced en dashes and semicolons for commas, as the house copy style requires."""
    text = re.sub(r"\s*\u2014\s*", ", ", text)
    text = re.sub(r"\s+\u2013\s+", ", ", text)
    return re.sub(r"\s*;\s*", ", ", text)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "y"}
    return bool(value)


class TriageDecision(BaseModel):
    category: Category = "Other"
    priority: Priority = "P3"
    sentiment: Sentiment = "neutral"
    team: Team | None = None
    summary: str = ""
    suggested_reply: str = ""
    next_steps: list[str] = Field(default_factory=list, validate_default=True)
    kb_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, validate_default=True)
    injection_suspected: bool = False
    escalate: bool = False
    escalation_reason: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any, info: ValidationInfo) -> str:
        matched = _match_choice(value, CATEGORIES)
        if matched is None:
            _note(info, f"category {value!r} is not in the taxonomy, used Other")
            return "Other"
        if matched != value:
            _note(info, f"category {value!r} normalised to {matched}")
        return matched

    @field_validator("priority", mode="before")
    @classmethod
    def _priority(cls, value: Any, info: ValidationInfo) -> str:
        key = str(value).strip().lower().replace(" ", "")
        if key in _PRIORITY_ALIASES:
            priority = _PRIORITY_ALIASES[key]
            if priority != value:
                _note(info, f"priority {value!r} normalised to {priority}")
            return priority
        _note(info, f"priority {value!r} is not recognised, used P3")
        return "P3"

    @field_validator("sentiment", mode="before")
    @classmethod
    def _sentiment(cls, value: Any, info: ValidationInfo) -> str:
        sentiment = _SENTIMENT_ALIASES.get(str(value).strip().lower())
        if sentiment is None:
            _note(info, f"sentiment {value!r} is not recognised, used neutral")
            return "neutral"
        if sentiment != value:
            _note(info, f"sentiment {value!r} normalised to {sentiment}")
        return sentiment

    @field_validator("team", mode="before")
    @classmethod
    def _team(cls, value: Any, info: ValidationInfo) -> str | None:
        matched = _match_choice(value, TEAMS)
        if matched is None and value is not None:
            _note(info, f"team {value!r} is not recognised, routed by category")
        return matched

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: Any, info: ValidationInfo) -> str:
        text = plain_punctuation(" ".join(str(value or "").split()))
        trimmed = trim_words(text, SUMMARY_MAX_WORDS)
        if trimmed != text:
            _note(info, f"summary trimmed to {SUMMARY_MAX_WORDS} words")
        return trimmed

    @field_validator("suggested_reply", mode="before")
    @classmethod
    def _reply(cls, value: Any, info: ValidationInfo) -> str:
        text = plain_punctuation(str(value or "").strip())
        trimmed = trim_reply(text)
        if trimmed != text:
            _note(info, f"suggested reply trimmed to {REPLY_MAX_WORDS} words")
        return trimmed

    @field_validator("next_steps", mode="before")
    @classmethod
    def _next_steps(cls, value: Any, info: ValidationInfo) -> list[str]:
        items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        steps = [
            plain_punctuation(" ".join(str(item).split()))[:NEXT_STEP_MAX_CHARS] for item in items if str(item).strip()
        ]
        if len(steps) > NEXT_STEPS_MAX:
            _note(info, f"next steps cut from {len(steps)} to {NEXT_STEPS_MAX}")
            steps = steps[:NEXT_STEPS_MAX]
        if not steps:
            _note(info, "next steps were empty, added a default step")
            steps = [DEFAULT_NEXT_STEP]
        return steps

    @field_validator("kb_refs", mode="before")
    @classmethod
    def _kb_refs(cls, value: Any) -> list[str]:
        items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        return list(dict.fromkeys(str(item).strip().lower() for item in items if str(item).strip()))

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any, info: ValidationInfo) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            _note(info, f"confidence {value!r} is not a number, used 0.5")
            return 0.5
        if math.isnan(number):
            _note(info, "confidence was not a number, used 0.5")
            return 0.5
        clamped = round(min(max(number, 0.0), 1.0), 2)
        if clamped != round(number, 2):
            _note(info, f"confidence {number} clamped to {clamped}")
        return clamped

    @field_validator("injection_suspected", "escalate", mode="before")
    @classmethod
    def _flag(cls, value: Any) -> bool:
        return _as_bool(value)

    @field_validator("escalation_reason", mode="before")
    @classmethod
    def _reason(cls, value: Any) -> str:
        return plain_punctuation(" ".join(str(value or "").split()))[:REASON_MAX_CHARS]

    @model_validator(mode="after")
    def _route_unknown_team(self) -> "TriageDecision":
        if self.team is None:
            self.team = DEFAULT_TEAM[self.category]
        return self
