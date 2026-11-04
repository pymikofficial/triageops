"""RMM intake settings, read once from environment variables.

Tests override attributes on ``rmm_settings`` with monkeypatch. The signing
secret is resolved at call time so a test can change either secret.
"""

from dataclasses import dataclass

from .. import config
from ..config import _integer, _text

DEFAULT_DEDUPE_MINUTES = 30
MIN_DEDUPE_MINUTES = 1
MAX_DEDUPE_MINUTES = 1440


def clamp_minutes(value: int) -> int:
    """Keep a de-duplication window between one minute and one day."""
    return max(MIN_DEDUPE_MINUTES, min(MAX_DEDUPE_MINUTES, value))


@dataclass
class RmmSettings:
    # Optional. When empty, WEBHOOK_SECRET signs RMM deliveries too.
    webhook_secret: str
    # Repeats of the same device and condition within this many minutes of the last one share a ticket.
    dedupe_minutes: int

    @classmethod
    def from_env(cls) -> "RmmSettings":
        return cls(
            webhook_secret=_text("RMM_WEBHOOK_SECRET"),
            dedupe_minutes=clamp_minutes(_integer("RMM_DEDUP_MINUTES", DEFAULT_DEDUPE_MINUTES)),
        )


rmm_settings = RmmSettings.from_env()


def signing_secret() -> str:
    """The HMAC secret for /api/webhooks/rmm: ``RMM_WEBHOOK_SECRET``, else ``WEBHOOK_SECRET``."""
    return rmm_settings.webhook_secret or config.settings.webhook_secret
