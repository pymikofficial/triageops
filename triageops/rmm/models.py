"""The channel neutral RMM alert and the TriageOps vocabulary for its severity.

Every parser produces an ``RmmAlert``. Severity uses our own five levels plus
``unknown``, so the priority hint and its reason never echo raw payload text.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

Severity = Literal["critical", "major", "moderate", "minor", "info", "unknown"]
AlertState = Literal["triggered", "reset"]
Vendor = Literal["ninjaone", "datto"]

SEVERITY_TO_PRIORITY: Final[dict[str, str]] = {
    "critical": "P1",
    "major": "P2",
    "moderate": "P3",
    "minor": "P4",
    "info": "P4",
}
SEVERITY_LABELS: Final[dict[str, str]] = {
    "critical": "Critical",
    "major": "Major",
    "moderate": "Moderate",
    "minor": "Minor",
    "info": "Information",
    "unknown": "Not supplied",
}
VENDOR_LABELS: Final[dict[str, str]] = {"ninjaone": "NinjaOne", "datto": "Datto RMM"}



@dataclass(frozen=True)
class RmmAlert:
    vendor: Vendor
    # Unique per delivery or occurrence, used for idempotency. None when the RMM sent no id.
    alert_id: str | None
    device_id: str
    device_name: str | None
    # The organisation or site the device belongs to.
    customer: str | None
    # Stable identity of the monitored condition, used for de-duplication.
    condition: str
    condition_name: str | None
    severity: Severity
    state: AlertState
    message: str
    # UTC aware. None when the RMM sent no usable time.
    occurred_at: datetime | None
    # Which vendor field gave the severity, in our words, for example "priority High".
    severity_reason: str | None = None

    @property
    def dedupe_key(self) -> str:
        """``vendor:sha256:<hash>`` of ``vendor:device:condition`` in lower case with whitespace collapsed.

        Always hashed, because the condition can come from free alert text and the key is stored in
        events that the dashboard shows. A hash keeps any secret in that text out of the audit trail.
        """
        key = " ".join(f"{self.vendor}:{self.device_id}:{self.condition}".lower().split())
        return f"{self.vendor}:sha256:{hashlib.sha256(key.encode()).hexdigest()[:40]}"

    @property
    def source_id(self) -> str | None:
        return f"rmm:{self.vendor}:{self.alert_id}" if self.alert_id else None


def priority_hint(alert: RmmAlert) -> str | None:
    """P1 to P4 from the RMM severity, or None when the RMM did not supply one."""
    return SEVERITY_TO_PRIORITY.get(alert.severity)


def priority_hint_reason(alert: RmmAlert) -> str | None:
    """Why the hint was given, for example ``"NinjaOne severity Critical"``. Built from our vocabulary only."""
    if priority_hint(alert) is None:
        return None
    basis = alert.severity_reason or f"severity {SEVERITY_LABELS[alert.severity]}"
    return f"{VENDOR_LABELS[alert.vendor]} {basis}"
