"""Autotask PSA adapter: ``POST {zone}/V1.0/Tickets`` with API user headers.

Every call sends ``ApiIntegrationCode``, ``UserName`` and ``Secret``. There is no
token. The REST base comes from ``AUTOTASK_BASE_URL`` or from zone discovery
(``zoneInformation?user=``, sent with no credentials, cached per adapter). The
version is pinned to V1.0 because V2.0 has no zone route.

``externalID`` carries the TriageOps ref, so after an ambiguous create the
adapter runs ``POST /Tickets/query`` with an ``externalID`` eq filter before it
sends again. Autotask reports validation and configuration faults as HTTP 500
with ``{"errors": [...]}``. Those texts are permanent and are never retried.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import PsaError, PsaResult, PsaTicket, error_reason, utc_iso
from .config import PsaSettings
from .retry import RetryBudget, create_with_lookup, send_with_retry

logger = logging.getLogger(__name__)

ZONE_URL = "https://webservices.autotask.net/atservicesrest/v1.0/zoneInformation"
TITLE_MAX_CHARS = 255
# The description limit is not documented. 8000 is the commonly reported value.
DESCRIPTION_MAX_CHARS = 8000
DEFAULT_DUE_HOURS = 4
CREDENTIAL_HINT = "Check AUTOTASK_USERNAME, AUTOTASK_SECRET and AUTOTASK_INTEGRATION_CODE"

# HTTP 500 texts that mark a permanent fault, from the Autotask error classification.
PERMANENT_MARKERS: tuple[str, ...] = (
    "integrationcode is invalid",
    "zone information could not be determined",
    "data violation",
    "check constraint",
    "is required",
    "is invalid",
    "does not exist",
)


def normalise_base_url(url: str) -> str:
    """``.../atservicesrest`` becomes ``.../atservicesrest/V1.0``. A URL already ending in V1.0 is kept."""
    base = url.strip().rstrip("/")
    return base if base.lower().endswith("/v1.0") else f"{base}/V1.0"


def _lower_keys(data: dict[str, Any]) -> dict[str, Any]:
    return {str(key).lower(): value for key, value in data.items()}


def _positive_id(value: Any) -> str | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return str(number) if number > 0 else None


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def parse_item_id(data: Any) -> str | None:
    """``{"itemId": n}``, with ``item.id`` and ``id`` as fallbacks."""
    if not isinstance(data, dict):
        return None
    fields = _lower_keys(data)
    if "itemid" in fields:
        return _positive_id(fields["itemid"])
    if isinstance(fields.get("item"), dict):
        return _positive_id(_lower_keys(fields["item"]).get("id"))
    return _positive_id(fields.get("id"))


class AutotaskAdapter:
    name = "autotask"
    label = "Autotask"

    def __init__(
        self,
        settings: PsaSettings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        zone_url: str = ZONE_URL,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.timeout_seconds)
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self.zone_url = zone_url
        self._base_url: str | None = (
            normalise_base_url(settings.autotask_base_url) if settings.autotask_base_url else None
        )
        self.web_url: str | None = None

    # Helpers ----------------------------------------------------------------------

    def _budget(self) -> RetryBudget:
        s = self.settings
        return RetryBudget(s.max_attempts, s.backoff_seconds, s.deadline_seconds, self._sleep, self._clock)

    def _secrets(self) -> tuple[str, ...]:
        s = self.settings
        return (s.autotask_secret, s.autotask_integration_code, s.autotask_username)

    def headers(self) -> dict[str, str]:
        s = self.settings
        return {
            "ApiIntegrationCode": s.autotask_integration_code,
            "UserName": s.autotask_username,
            "Secret": s.autotask_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _send(self, method: str, url: str, budget: RetryBudget, *, idempotent: bool, **kwargs: Any) -> httpx.Response:
        response, _ = send_with_retry(
            self.client, method, url, idempotent=idempotent, budget=budget, label=self.label,
            permanent_markers=PERMANENT_MARKERS, headers=self.headers(), **kwargs,
        )  # fmt: skip
        return response

    # Zone -------------------------------------------------------------------------

    def base_url(self, budget: RetryBudget) -> str:
        """The configured REST base, or the zone URL discovered once and then cached."""
        if self._base_url:
            return self._base_url
        response, _ = send_with_retry(
            self.client, "GET", self.zone_url, idempotent=True, budget=budget.child(), label=self.label,
            permanent_markers=PERMANENT_MARKERS, params={"user": self.settings.autotask_username},
            headers={"Accept": "application/json"},
        )  # fmt: skip
        if not response.is_success:
            reason = error_reason("Autotask zone discovery failed", response, secrets=self._secrets())
            raise PsaError(
                f"{reason}. Check AUTOTASK_USERNAME or set AUTOTASK_BASE_URL",
                status_code=response.status_code,
                attempts=budget.attempts,
                retryable=response.status_code in (429, 502, 503, 504),
            )
        data = _json(response)
        url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not self._trusted_zone(url):
            raise PsaError("Autotask zone discovery returned an unexpected URL. Set AUTOTASK_BASE_URL")
        web_url = data.get("webUrl")
        self.web_url = web_url if isinstance(web_url, str) else None
        self._base_url = normalise_base_url(url)
        return self._base_url

    @staticmethod
    def _trusted_zone(url: str) -> bool:
        """The secret is sent to this URL, so only https Autotask hosts are accepted."""
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        return parts.scheme.lower() == "https" and (host == "autotask.net" or host.endswith(".autotask.net"))

    # Tickets ----------------------------------------------------------------------

    def ticket_body(self, ticket: PsaTicket) -> dict[str, Any]:
        """The flat ticket object. ``companyID`` 0 is the MSP itself and is always sent."""
        s = self.settings
        due = ticket.sla_due_at or self._now() + timedelta(hours=DEFAULT_DUE_HOURS)
        priority = ticket.priority if ticket.priority in s.autotask_priority_ids else "P3"
        body: dict[str, Any] = {
            "companyID": s.autotask_company_id,
            "title": ticket.title[:TITLE_MAX_CHARS],
            "description": ticket.description(DESCRIPTION_MAX_CHARS),
            "status": s.autotask_status_id,
            "priority": s.autotask_priority_ids[priority],
            "dueDateTime": utc_iso(due),
            "externalID": ticket.ref,
        }
        for key, value in (
            ("queueID", s.autotask_queue_id),
            ("source", s.autotask_source_id),
            ("ticketType", s.autotask_ticket_type_id),
        ):
            if value is not None:
                body[key] = value
        return body

    def find_ticket(self, ref: str, budget: RetryBudget) -> str | None:
        """The id of the Autotask ticket whose ``externalID`` is our ref, or None. Raises if the query fails."""
        query = {"filter": [{"op": "eq", "field": "externalID", "value": ref}], "maxRecords": 1}
        response = self._send("POST", f"{self.base_url(budget)}/Tickets/query", budget, idempotent=True, json=query)
        if not response.is_success:
            raise PsaError(error_reason("Autotask ticket query failed", response, secrets=self._secrets()))
        data = _json(response)
        items = _lower_keys(data).get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise PsaError("Autotask ticket query returned an unexpected answer")
        for item in items:
            fields = _lower_keys(item) if isinstance(item, dict) else {}
            if "externalid" not in fields:
                raise PsaError("Autotask ticket query did not return the externalID field")
            if str(fields.get("externalid") or "").strip() == ref:
                found = _positive_id(fields.get("id"))
                if found:
                    return found
        return None

    def create_ticket(self, ticket: PsaTicket) -> PsaResult:
        budget = self._budget()
        url = f"{self.base_url(budget)}/Tickets"
        payload = self.ticket_body(ticket)
        outcome = create_with_lookup(
            post=lambda shared: self._send("POST", url, shared, idempotent=False, json=payload),
            lookup=lambda shared: self.find_ticket(ticket.ref, shared),
            budget=budget,
            label=self.label,
            permanent_markers=PERMANENT_MARKERS,
        )
        if outcome.found_id:
            return self._result(outcome.found_id, outcome.attempts)
        response = outcome.response
        if response is None:
            raise PsaError("Autotask returned no answer", attempts=outcome.attempts)
        if response.is_success:
            created = parse_item_id(_json(response))
            return self._result(created or self._find_after_missing_id(ticket.ref, budget), outcome.attempts)
        raise self._create_error(response, outcome.attempts)

    def _find_after_missing_id(self, ref: str, budget: RetryBudget) -> str:
        """Autotask answered 2xx without an itemId. Look for the ticket, never send it again."""
        try:
            found = self.find_ticket(ref, budget.child())
        except PsaError:
            found = None
        if found:
            return found
        raise PsaError(
            "Autotask accepted the request but returned no itemId. "
            "The ticket may have been created, so check Autotask before retrying",
            attempts=budget.attempts,
            ambiguous=True,
        )

    def _create_error(self, response: httpx.Response, attempts: int) -> PsaError:
        status = response.status_code
        if status in (401, 403):
            reason = error_reason("Autotask rejected the API credentials", response, secrets=self._secrets())
            return PsaError(f"{reason}. {CREDENTIAL_HINT}", status_code=status, attempts=attempts)
        return PsaError(
            error_reason("Autotask rejected the ticket", response, secrets=self._secrets()),
            status_code=status,
            attempts=attempts,
            retryable=status in (429, 503),
        )

    def _result(self, ticket_id: str, attempts: int) -> PsaResult:
        logger.info("psa ticket created provider=autotask id=%s attempts=%s", ticket_id, attempts)
        # Autotask documents no stable link to a ticket detail page, so no URL is stored.
        return PsaResult(provider=self.name, external_id=ticket_id, external_ref=None, url=None, attempts=attempts)
