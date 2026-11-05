"""Turn NinjaOne and Datto RMM webhook payloads into an ``RmmAlert``.

Payloads are parsed defensively. A field with the wrong type is skipped, text
is stripped and length capped, and only a missing device or condition identity
is an error. Neither vendor signs its webhooks, so these payloads reach
TriageOps through the n8n relay, which signs them with our timestamped HMAC.

NinjaOne sends one activity object per request. Only condition activities
(``CONDITION``, ``CONDITION_ACTION`` and ``CONDITION_ACTIONSET``) are alerts.
``TRIGGERED`` raises, ``RESET`` and ``RESET_BY_PSA_TICKET_CALLBACK`` clear, and
any other status, such as ``ACKNOWLEDGED``, is ignored.

Datto RMM sends a body the MSP writes as a template, with ``[variable]`` tokens
replaced by plain text. Keys are matched without regard to case, a value that
is still an unexpanded ``[placeholder]`` counts as missing, and the Datto API
v2 alert object is accepted as well.
"""

import hashlib
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .models import AlertState, RmmAlert, Severity, Vendor

ID_MAX_CHARS = 120
NAME_MAX_CHARS = 160
MESSAGE_MAX_CHARS = 3000
_ISO_MAX_CHARS = 64

# Values for the optional ?source= query parameter.
SOURCE_ALIASES: dict[str, Vendor] = {"ninjaone": "ninjaone", "ninja": "ninjaone", "datto": "datto", "dattormm": "datto"}

# Keys only a NinjaOne activity carries (exact case, as NinjaOne sends them).
_NINJA_MARKERS = frozenset({"activityType", "statusCode", "sourceConfigUid", "seriesUid", "activityTime"})
# Keys a Datto RMM template or API alert carries (compared in lower case).
_DATTO_MARKERS = frozenset(
    {
        "alert_uid", "alertuid", "device_uid", "deviceuid", "device_hostname", "alert_type", "alert_category",
        "alert_message", "alertmessage", "alertsourceinfo", "alertcontext", "site_name", "sitename", "alerts",
    }
)  # fmt: skip

_PLACEHOLDER = re.compile(r"^\[[A-Za-z0-9_\-]+\]$")
_STATUS_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,59}$")
_DIGITS = re.compile(r"\d+")


class RmmPayloadError(ValueError):
    """The payload cannot become an alert. The message is safe to return to the client."""


class RmmIgnored(Exception):
    """A valid delivery that TriageOps does not act on, such as an acknowledgement."""

    def __init__(self, vendor: Vendor, reason: str) -> None:
        super().__init__(reason)
        self.vendor: Vendor = vendor
        self.reason = reason


# Value helpers ---------------------------------------------------------------------

def _clean(value: Any, limit: int, *, placeholders: bool = False) -> str | None:
    """A single line of text, or None for a missing, blank or wrongly typed value."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, float):
        if not (math.isfinite(value) and value.is_integer()):
            return None
        value = str(int(value))
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or (placeholders and _PLACEHOLDER.match(text)):
        return None
    return text[:limit].rstrip()


def _message(value: Any, *, placeholders: bool = False) -> str | None:
    """Multi line text, stripped and capped, or None."""
    if not isinstance(value, str):
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or (placeholders and _PLACEHOLDER.match(text)):
        return None
    return text[:MESSAGE_MAX_CHARS].rstrip()


def _upper(value: Any) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _lower_keys(value: Any) -> dict[str, Any]:
    return {key.lower(): item for key, item in _mapping(value).items() if isinstance(key, str)}


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value) if value in (0, 1) else None
    text = value.strip().lower() if isinstance(value, str) else ""
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0"):
        return False
    return None


def _condition_from_text(text: str | None) -> str | None:
    """A stable identity from free text: lower case, digits stripped, whitespace collapsed."""
    if not text:
        return None
    stripped = " ".join(_DIGITS.sub("", text).lower().split())
    if not stripped:
        return None
    if len(stripped) > NAME_MAX_CHARS:
        return "text:" + hashlib.sha256(stripped.encode()).hexdigest()[:32]
    return stripped


def _from_iso(text: str) -> datetime | None:
    if len(text) > _ISO_MAX_CHARS:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_time(value: Any) -> datetime | None:
    """Epoch seconds or milliseconds (above 1e12), as a number or numeric string, else ISO 8601."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            return _from_iso(text)
    else:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number > 1e12:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, UTC)
    except (OverflowError, OSError, ValueError):
        return None


# Vendor detection ------------------------------------------------------------------

def detect_vendor(payload: Any, source: str | None = None) -> Vendor:
    """The explicit ``source`` query parameter wins, otherwise the payload shape decides."""
    if not isinstance(payload, Mapping):
        raise RmmPayloadError("The request body must be a JSON object.")
    if source and source.strip():
        vendor = SOURCE_ALIASES.get(source.strip().lower())
        if vendor is None:
            raise RmmPayloadError("Unknown source. Use ninjaone or datto.")
        return vendor
    if _NINJA_MARKERS & payload.keys():
        return "ninjaone"
    if _DATTO_MARKERS & _lower_keys(payload).keys():
        return "datto"
    raise RmmPayloadError("The alert format was not recognised. Add ?source=ninjaone or ?source=datto to the URL.")


# NinjaOne --------------------------------------------------------------------------

_NINJA_ALERT_ACTIVITIES = frozenset({"CONDITION", "CONDITION_ACTION", "CONDITION_ACTIONSET"})
_NINJA_RAISED = frozenset({"TRIGGERED"})
_NINJA_CLEARED = frozenset({"RESET", "RESET_BY_PSA_TICKET_CALLBACK"})
_NINJA_SEVERITY: dict[str, tuple[Severity, str]] = {
    "CRITICAL": ("critical", "Critical"),
    "MAJOR": ("major", "Major"),
    "MODERATE": ("moderate", "Moderate"),
    "MINOR": ("minor", "Minor"),
    "NONE": ("info", "None"),
}
_NINJA_PRIORITY: dict[str, tuple[Severity, str]] = {
    "HIGH": ("major", "High"),
    "MEDIUM": ("moderate", "Medium"),
    "LOW": ("minor", "Low"),
    "NONE": ("info", "None"),
}


def _ninja_severity(payload: Mapping[str, Any]) -> tuple[Severity, str | None]:
    """Severity first. Priority is used only when severity is NONE or missing."""
    severity = _NINJA_SEVERITY.get(_upper(payload.get("severity")))
    if severity and severity[0] != "info":
        return severity[0], f"severity {severity[1]}"
    priority = _NINJA_PRIORITY.get(_upper(payload.get("priority")))
    if priority and priority[0] != "info":
        return priority[0], f"priority {priority[1]}"
    if severity:
        return "info", f"severity {severity[1]}"
    if priority:
        return "info", f"priority {priority[1]}"
    return "unknown", None


def _ninja_state(code: str) -> AlertState:
    if code in _NINJA_RAISED:
        return "triggered"
    if code in _NINJA_CLEARED:
        return "reset"
    if _STATUS_CODE.match(code):
        raise RmmIgnored("ninjaone", f"NinjaOne status {code} does not raise or clear an alert")
    raise RmmIgnored("ninjaone", "The NinjaOne activity has no status that raises or clears an alert")


def parse_ninjaone(payload: Any) -> RmmAlert:
    """Build an alert from a NinjaOne activity webhook (API webhook or console channel shape)."""
    if not isinstance(payload, Mapping):
        raise RmmPayloadError("The request body must be a JSON object.")
    activity = _upper(payload.get("activityType"))
    if activity and activity not in _NINJA_ALERT_ACTIVITIES:
        raise RmmIgnored("ninjaone", "The NinjaOne activity is not an alert condition")
    # statusCode carries TRIGGERED or RESET. The status display label is only a fallback.
    code = _upper(payload.get("statusCode")) or _upper(payload.get("status"))
    state = _ninja_state(code)

    device = _mapping(payload.get("device"))
    device_id = _clean(payload.get("deviceId"), ID_MAX_CHARS) or _clean(device.get("id"), ID_MAX_CHARS)
    if not device_id:
        raise RmmPayloadError("The NinjaOne alert has no deviceId.")
    device_name = next(
        (name for key in ("displayName", "systemName", "dnsName") if (name := _clean(device.get(key), NAME_MAX_CHARS))),
        f"device {device_id}",
    )
    organisation = _mapping(_mapping(device.get("references")).get("organization"))

    source_name = _clean(payload.get("sourceName"), NAME_MAX_CHARS)
    subject = _clean(payload.get("subject"), NAME_MAX_CHARS)
    message = _message(payload.get("message")) or subject or source_name or ""
    condition = (
        _clean(payload.get("sourceConfigUid"), ID_MAX_CHARS)
        or source_name
        or subject
        or _condition_from_text(message)
    )
    if not condition:
        raise RmmPayloadError("The NinjaOne alert has no sourceConfigUid, sourceName, subject or message.")

    activity_id = _clean(payload.get("id"), ID_MAX_CHARS)
    severity, severity_reason = _ninja_severity(payload)
    return RmmAlert(
        vendor="ninjaone",
        alert_id=f"{activity_id}:{code}" if activity_id else None,
        device_id=device_id,
        device_name=device_name,
        customer=_clean(organisation.get("name"), NAME_MAX_CHARS),
        condition=condition,
        condition_name=source_name or subject,
        severity=severity,
        state=state,
        message=message,
        occurred_at=parse_time(payload.get("activityTime")),
        severity_reason=severity_reason,
    )


# Datto RMM -------------------------------------------------------------------------

_DATTO_PRIORITY: dict[str, tuple[Severity, str]] = {
    "critical": ("critical", "Critical"),
    "high": ("major", "High"),
    "moderate": ("moderate", "Moderate"),
    "low": ("minor", "Low"),
    "information": ("info", "Information"),
}
_RESOLVED_WORDS = frozenset({"resolved", "resolve", "cleared", "reset", "closed"})
_RAISED_WORDS = frozenset({"triggered", "raised", "open", "opened", "created"})
# Fields of alertContext that tell two conditions of the same class apart, such as CPU and memory usage.
_CONTEXT_DISCRIMINATORS = ("type", "processname", "diskname")
_CONTEXT_LABELS = {
    "online_offline_status_ctx": "Device online status",
    "perf_resource_usage_ctx": "Resource usage",
    "eventlog_ctx": "Event log alert",
    "comp_script_ctx": "Component monitor",
    "patch_ctx": "Patch alert",
    "antivirus_ctx": "Antivirus alert",
}


def _first(data: Mapping[str, Any], keys: tuple[str, ...], limit: int) -> str | None:
    for key in keys:
        value = _clean(data.get(key), limit, placeholders=True)
        if value:
            return value
    return None


def _datto_severity(data: Mapping[str, Any]) -> tuple[Severity, str | None]:
    for key in ("priority", "alert_priority", "severity"):
        value = _clean(data.get(key), ID_MAX_CHARS, placeholders=True)
        mapped = _DATTO_PRIORITY.get(value.lower()) if value else None
        if mapped:
            return mapped[0], f"priority {mapped[1]}"
    return "unknown", None


def _datto_state(data: Mapping[str, Any]) -> AlertState:
    """A body discriminator written by the MSP, or the API ``resolved`` flag. Raised by default."""
    for key in ("event", "status", "alertlevel"):
        word = (_clean(data.get(key), ID_MAX_CHARS, placeholders=True) or "").lower()
        if word in _RESOLVED_WORDS:
            return "reset"
        if word in _RAISED_WORDS:
            return "triggered"
    for key in ("alert_resolved", "alertresolved", "resolved"):
        flag = _flag(data.get(key))
        if flag is not None:
            return "reset" if flag else "triggered"
    return "triggered"


def _datto_context_condition(context: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Condition identity and label from the API ``alertContext`` object."""
    context_class = _clean(context.get("@class"), ID_MAX_CHARS)
    if not context_class:
        return None, None
    parts = [context_class.lower()]
    parts += [value.lower() for key in _CONTEXT_DISCRIMINATORS if (value := _clean(context.get(key), ID_MAX_CHARS))]
    label = _CONTEXT_LABELS.get(context_class.lower())
    return ":".join(parts), label


def parse_datto(payload: Any) -> RmmAlert:
    """Build an alert from a Datto RMM webhook template body or an API v2 alert object."""
    if not isinstance(payload, Mapping):
        raise RmmPayloadError("The request body must be a JSON object.")
    data = _lower_keys(payload)
    if isinstance(data.get("alerts"), list):
        raise RmmPayloadError("Send one Datto RMM alert per request. The relay must split a list of alerts.")
    source_info = _lower_keys(data.get("alertsourceinfo"))
    context = _lower_keys(data.get("alertcontext"))

    hostname = _first(data, ("device_hostname", "hostname"), NAME_MAX_CHARS) or _first(
        source_info, ("devicename",), NAME_MAX_CHARS
    )
    device_id = (
        _first(data, ("device_uid", "deviceuid", "device_id"), ID_MAX_CHARS)
        or _first(source_info, ("deviceuid",), ID_MAX_CHARS)
        or hostname
    )
    if not device_id:
        raise RmmPayloadError(
            "The Datto RMM alert has no device_uid, device_id or hostname. Unexpanded placeholders count as missing."
        )

    alert_text = _first(data, ("alert",), NAME_MAX_CHARS)
    message = (
        next(
            (
                text
                for key in ("alert_message", "alertmessage", "alert_message_en", "diagnostics")
                if (text := _message(data.get(key), placeholders=True))
            ),
            None,
        )
        or alert_text
        or ""
    )
    alert_type = _first(data, ("alert_type", "alerttype"), NAME_MAX_CHARS)
    alert_category = _first(data, ("alert_category", "alertcategory"), NAME_MAX_CHARS)
    context_condition, context_label = _datto_context_condition(context)
    condition = (
        "/".join(part for part in (alert_type, alert_category) if part)
        or context_condition
        or _condition_from_text(alert_text)
        or _condition_from_text(message)
    )
    if not condition:
        raise RmmPayloadError(
            "The Datto RMM alert has no alert_type, alert_category, alertContext, alert or alert_message."
        )
    first_line = message.split("\n", 1)[0][:NAME_MAX_CHARS].rstrip() or None
    condition_name = alert_type or alert_category or alert_text or first_line or context_label

    state = _datto_state(data)
    alert_uid = _first(data, ("alert_uid", "alertuid"), ID_MAX_CHARS)
    severity, severity_reason = _datto_severity(data)
    return RmmAlert(
        vendor="datto",
        # Datto keeps the same alert uid for the raised and the resolved send, so the state is part of the id.
        alert_id=f"{alert_uid}:{'resolved' if state == 'reset' else 'raised'}" if alert_uid else None,
        device_id=device_id,
        device_name=hostname,
        customer=_first(data, ("site_name", "sitename"), NAME_MAX_CHARS)
        or _first(source_info, ("sitename",), NAME_MAX_CHARS),
        condition=condition,
        condition_name=condition_name,
        severity=severity,
        state=state,
        message=message,
        occurred_at=parse_time(data.get("timestamp")),
        severity_reason=severity_reason,
    )


# Dispatch --------------------------------------------------------------------------

def parse_alert(payload: Any, source: str | None = None) -> RmmAlert:
    """Detect the vendor and parse. Raises ``RmmPayloadError`` or ``RmmIgnored``."""
    if detect_vendor(payload, source) == "ninjaone":
        return parse_ninjaone(payload)
    return parse_datto(payload)
