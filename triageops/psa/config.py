"""PSA settings, read once from environment variables.

PSA sync is off unless ``PSA_PROVIDER`` is ``halopsa`` or ``autotask``. Every
value is server side only and is never sent to the browser or written to an
event. Tests override attributes on ``psa_settings`` with monkeypatch, or build
their own instance with ``PsaSettings.from_env({...})``.

Integer settings that do not parse, priority maps that do not parse and base
URLs that do not use https are recorded as problems. A provider with any
problem or missing value is not ready, and the sync writes ``psa_skipped``
naming the settings to fix instead of calling the PSA.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PROVIDERS: tuple[str, ...] = ("halopsa", "autotask")
PROVIDER_LABELS: dict[str, str] = {"halopsa": "HaloPSA", "autotask": "Autotask"}
PRIORITY_KEYS: tuple[str, ...] = ("P1", "P2", "P3", "P4")

DEFAULT_HALOPSA_PRIORITY_IDS = "P1:1,P2:2,P3:3,P4:4"
# The Autotask default picklist: 4 Critical, 1 High, 2 Medium, 3 Low.
DEFAULT_AUTOTASK_PRIORITY_IDS = "P1:4,P2:1,P3:2,P4:3"

_MAX_ATTEMPTS_RANGE = (1, 5)


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def _number(env: Mapping[str, str], name: str, default: float, problems: dict[str, str]) -> float:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        problems[name] = "must be a number"
        return default
    if value <= 0:
        problems[name] = "must be greater than zero"
        return default
    return value


def _clamped_int(env: Mapping[str, str], name: str, default: int, bounds: tuple[int, int], problems: dict[str, str]) -> int:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        problems[name] = "must be a whole number"
        return default
    return min(max(value, bounds[0]), bounds[1])


def _optional_int(env: Mapping[str, str], name: str, problems: dict[str, str]) -> int | None:
    """An optional whole number. Zero is a valid value and is kept."""
    raw = _text(env, name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        problems[name] = "must be a whole number"
        return None


def _https_url(env: Mapping[str, str], name: str, problems: dict[str, str]) -> str:
    """A base URL without a trailing slash. Anything but https is a problem, because secrets are sent there."""
    raw = _text(env, name).rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https" or not parts.netloc:
        problems[name] = "must be an https URL"
    return raw


def parse_priority_ids(raw: str, default: str) -> dict[str, int]:
    """Parse ``P1:4,P2:1`` into a map. Entries given override the defaults. Raises ``ValueError``."""
    result = _parse_pairs(default)
    if raw.strip():
        result.update(_parse_pairs(raw))
    return result


def _parse_pairs(raw: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for entry in raw.split(","):
        if not entry.strip():
            continue
        key, sep, value = entry.partition(":")
        key = key.strip().upper()
        if not sep or key not in PRIORITY_KEYS:
            raise ValueError(f"unknown priority entry {entry.strip()[:20]!r}")
        result[key] = int(value.strip())
    return result


def _priority_map(env: Mapping[str, str], name: str, default: str, problems: dict[str, str]) -> dict[str, int]:
    try:
        return parse_priority_ids(_text(env, name), default)
    except ValueError:
        problems[name] = "must look like P1:1,P2:2,P3:3,P4:4"
        return parse_priority_ids("", default)


def _provider(env: Mapping[str, str]) -> str:
    provider = _text(env, "PSA_PROVIDER").lower()
    if provider and provider not in PROVIDERS:
        logger.warning("PSA_PROVIDER %r is not supported, PSA sync is off", provider[:20])
        return ""
    return provider


@dataclass
class PsaSettings:
    provider: str = ""
    timeout_seconds: float = 8.0
    max_attempts: int = 3
    backoff_seconds: float = 0.5
    deadline_seconds: float = 20.0

    halopsa_base_url: str = ""
    halopsa_auth_url: str = ""
    halopsa_tenant: str = ""
    halopsa_client_id: str = ""
    halopsa_client_secret: str = ""
    halopsa_scope: str = "all"
    halopsa_ticket_type_id: int | None = None
    halopsa_default_client_id: int | None = None
    halopsa_site_id: int | None = None
    halopsa_team_id: int | None = None
    halopsa_priority_ids: dict[str, int] = field(
        default_factory=lambda: parse_priority_ids("", DEFAULT_HALOPSA_PRIORITY_IDS)
    )

    autotask_username: str = ""
    autotask_secret: str = ""
    autotask_integration_code: str = ""
    autotask_base_url: str = ""
    autotask_company_id: int | None = None
    autotask_queue_id: int | None = None
    autotask_ticket_type_id: int | None = None
    autotask_source_id: int | None = None
    autotask_status_id: int = 1
    autotask_priority_ids: dict[str, int] = field(
        default_factory=lambda: parse_priority_ids("", DEFAULT_AUTOTASK_PRIORITY_IDS)
    )

    # Env var name to the reason its value was rejected.
    invalid: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PsaSettings":
        env = os.environ if env is None else env
        problems: dict[str, str] = {}
        status_id = _optional_int(env, "AUTOTASK_STATUS_ID", problems)
        return cls(
            provider=_provider(env),
            timeout_seconds=_number(env, "PSA_TIMEOUT_SECONDS", 8.0, problems),
            max_attempts=_clamped_int(env, "PSA_MAX_ATTEMPTS", 3, _MAX_ATTEMPTS_RANGE, problems),
            backoff_seconds=_number(env, "PSA_BACKOFF_SECONDS", 0.5, problems),
            deadline_seconds=_number(env, "PSA_DEADLINE_SECONDS", 20.0, problems),
            halopsa_base_url=_https_url(env, "HALOPSA_BASE_URL", problems),
            halopsa_auth_url=_https_url(env, "HALOPSA_AUTH_URL", problems),
            halopsa_tenant=_text(env, "HALOPSA_TENANT"),
            halopsa_client_id=_text(env, "HALOPSA_CLIENT_ID"),
            halopsa_client_secret=_text(env, "HALOPSA_CLIENT_SECRET"),
            halopsa_scope=_text(env, "HALOPSA_SCOPE") or "all",
            halopsa_ticket_type_id=_optional_int(env, "HALOPSA_TICKET_TYPE_ID", problems),
            halopsa_default_client_id=_optional_int(env, "HALOPSA_DEFAULT_CLIENT_ID", problems),
            halopsa_site_id=_optional_int(env, "HALOPSA_SITE_ID", problems),
            halopsa_team_id=_optional_int(env, "HALOPSA_TEAM_ID", problems),
            halopsa_priority_ids=_priority_map(env, "HALOPSA_PRIORITY_IDS", DEFAULT_HALOPSA_PRIORITY_IDS, problems),
            autotask_username=_text(env, "AUTOTASK_USERNAME"),
            autotask_secret=_text(env, "AUTOTASK_SECRET"),
            autotask_integration_code=_text(env, "AUTOTASK_INTEGRATION_CODE"),
            autotask_base_url=_https_url(env, "AUTOTASK_BASE_URL", problems),
            autotask_company_id=_optional_int(env, "AUTOTASK_COMPANY_ID", problems),
            autotask_queue_id=_optional_int(env, "AUTOTASK_QUEUE_ID", problems),
            autotask_ticket_type_id=_optional_int(env, "AUTOTASK_TICKET_TYPE_ID", problems),
            autotask_source_id=_optional_int(env, "AUTOTASK_SOURCE_ID", problems),
            autotask_status_id=1 if status_id is None else status_id,
            autotask_priority_ids=_priority_map(
                env, "AUTOTASK_PRIORITY_IDS", DEFAULT_AUTOTASK_PRIORITY_IDS, problems
            ),
            invalid=problems,
        )

    # Derived values ---------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.provider in PROVIDERS

    @property
    def label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, "the PSA")

    @property
    def halopsa_token_url(self) -> str:
        auth = (self.halopsa_auth_url or f"{self.halopsa_base_url}/auth").rstrip("/")
        return f"{auth}/token"

    def _required(self) -> dict[str, Any]:
        if self.provider == "halopsa":
            return {
                "HALOPSA_BASE_URL": self.halopsa_base_url,
                "HALOPSA_CLIENT_ID": self.halopsa_client_id,
                "HALOPSA_CLIENT_SECRET": self.halopsa_client_secret,
                "HALOPSA_TICKET_TYPE_ID": self.halopsa_ticket_type_id,
            }
        if self.provider == "autotask":
            return {
                "AUTOTASK_USERNAME": self.autotask_username,
                "AUTOTASK_SECRET": self.autotask_secret,
                "AUTOTASK_INTEGRATION_CODE": self.autotask_integration_code,
                "AUTOTASK_COMPANY_ID": self.autotask_company_id,
            }
        return {}

    def _relevant(self, name: str) -> bool:
        prefix = {"halopsa": "HALOPSA_", "autotask": "AUTOTASK_"}.get(self.provider)
        return name.startswith("PSA_") or bool(prefix and name.startswith(prefix))

    def missing(self) -> list[str]:
        """Env var names the provider still needs: unset, or set to a value that was rejected."""
        if not self.enabled:
            return []
        # Zero is a valid id (the Autotask company of the MSP itself), so only None and "" count as unset.
        unset = [name for name, value in self._required().items() if value is None or value == ""]
        rejected = [name for name in self.invalid if self._relevant(name) and name not in unset]
        return unset + rejected

    def problems(self) -> list[str]:
        """``missing()`` with the reason a value was rejected, for the ``psa_skipped`` event."""
        return [f"{name} ({self.invalid[name]})" if name in self.invalid else name for name in self.missing()]

    @property
    def ready(self) -> bool:
        return self.enabled and not self.missing()

    def cache_key(self) -> tuple[Any, ...]:
        """Hashable identity of the values an adapter is built from, so a change builds a new adapter."""
        values = []
        for name, value in sorted(vars(self).items()):
            if isinstance(value, dict):
                value = tuple(sorted(value.items()))
            values.append((name, value))
        return tuple(values)


psa_settings = PsaSettings.from_env()
