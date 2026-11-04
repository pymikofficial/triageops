"""HaloPSA adapter: client credentials token, then ``POST /api/Tickets``.

The token comes from ``{auth_url}/token`` as a form post (``?tenant=`` only when
``HALOPSA_TENANT`` is set). It is cached until 60 seconds before it expires and
refreshed once when the resource server answers 401.

Halo takes a JSON array holding one ticket. ``third_party_id_string`` carries
the TriageOps ref, so after an ambiguous create (a timeout or a 5xx) the
adapter searches ``GET /api/Tickets?third_party_id_string=<ref>`` before it
sends again. The create answer comes in three documented shapes (the ticket,
a list holding it, or ``{"ticket": {...}, "success": ...}``), all accepted.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from .base import PsaError, PsaResult, PsaTicket, error_reason, utc_iso
from .config import PsaSettings
from .retry import RetryBudget, create_with_lookup, send_with_retry

logger = logging.getLogger(__name__)

# Halo does not publish the summary length. 250 keeps a long subject safe and is never reached in practice.
SUMMARY_MAX_CHARS = 250
TOKEN_REFRESH_MARGIN_SECONDS = 60
DEFAULT_TOKEN_SECONDS = 3600
REPORTED_BY = "TriageOps"
CREDENTIAL_HINT = "Check HALOPSA_CLIENT_ID, HALOPSA_CLIENT_SECRET and HALOPSA_TENANT"


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


def parse_created_id(data: Any) -> str | None:
    """The ticket id from any documented create answer, or None when there is none."""
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    lowered = _lower_keys(data)
    if isinstance(lowered.get("ticket"), dict):
        if lowered.get("success") is False:
            logger.warning("halopsa reported success false for a created ticket")
        lowered = _lower_keys(lowered["ticket"])
    return _positive_id(lowered.get("id"))


def _search_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = _lower_keys(data).get("tickets")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


class HaloPsaAdapter:
    name = "halopsa"
    label = "HaloPSA"

    def __init__(
        self,
        settings: PsaSettings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.timeout_seconds)
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0

    # Helpers ----------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self.settings.halopsa_base_url.rstrip("/")

    def _budget(self) -> RetryBudget:
        s = self.settings
        return RetryBudget(s.max_attempts, s.backoff_seconds, s.deadline_seconds, self._sleep, self._clock)

    def _secrets(self) -> tuple[str, ...]:
        return (self.settings.halopsa_client_secret, self.settings.halopsa_client_id, self._token or "")

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def ticket_url(self, ticket_id: str) -> str:
        return f"{self.base_url}/tickets?id={ticket_id}"

    # Token ------------------------------------------------------------------------

    def _invalidate_token(self) -> None:
        self._token, self._token_expires_at = None, 0.0

    def get_token(self, budget: RetryBudget) -> str:
        """The cached token, or a new one from the authorisation server."""
        if self._token and self._clock() < self._token_expires_at:
            return self._token
        s = self.settings
        request: dict[str, Any] = {
            "data": {
                "grant_type": "client_credentials",
                "client_id": s.halopsa_client_id,
                "client_secret": s.halopsa_client_secret,
                "scope": s.halopsa_scope or "all",
            },
            "headers": {"Accept": "application/json"},
        }
        if s.halopsa_tenant:
            request["params"] = {"tenant": s.halopsa_tenant}
        response, _ = send_with_retry(
            self.client, "POST", s.halopsa_token_url, idempotent=True, budget=budget.child(), label=self.label,
            **request,
        )  # fmt: skip
        data = _json(response) if response.is_success else None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            prefix = "HaloPSA authentication failed"
            reason = error_reason(prefix, response, secrets=self._secrets()) if not response.is_success else (
                f"{prefix}, the answer held no access token"
            )
            raise PsaError(
                f"{reason}. {CREDENTIAL_HINT}",
                status_code=response.status_code,
                attempts=budget.attempts,
                retryable=response.status_code in (429, 502, 503, 504),
            )
        try:
            lifetime = float(data.get("expires_in", DEFAULT_TOKEN_SECONDS))
        except (TypeError, ValueError):
            lifetime = DEFAULT_TOKEN_SECONDS
        self._token = token.strip()
        self._token_expires_at = self._clock() + max(0.0, lifetime - TOKEN_REFRESH_MARGIN_SECONDS)
        return self._token

    # Tickets ----------------------------------------------------------------------

    def ticket_body(self, ticket: PsaTicket) -> dict[str, Any]:
        """One Halo ``Faults`` object. Optional ids are sent only when configured."""
        s = self.settings
        body: dict[str, Any] = {
            "summary": ticket.title[:SUMMARY_MAX_CHARS],
            "details": ticket.description(),
            "tickettype_id": s.halopsa_ticket_type_id,
            "third_party_id_string": ticket.ref,
            "reportedby": REPORTED_BY,
        }
        if ticket.priority in s.halopsa_priority_ids:
            body["priority_id"] = s.halopsa_priority_ids[ticket.priority]
        if ticket.category:
            body["category_1"] = ticket.category
        # A configured team id wins over the triaged team name, so the two never disagree.
        if s.halopsa_team_id is not None:
            body["team_id"] = s.halopsa_team_id
        elif ticket.team:
            body["team"] = ticket.team
        if s.halopsa_default_client_id is not None:
            body["client_id"] = s.halopsa_default_client_id
        if s.halopsa_site_id is not None:
            body["site_id"] = s.halopsa_site_id
        if ticket.created_at:
            body["dateoccurred"] = utc_iso(ticket.created_at, milliseconds=True)
        return body

    def find_ticket(self, ref: str, budget: RetryBudget) -> str | None:
        """The id of the Halo ticket that carries our ref, or None. Raises ``PsaError`` if the search fails."""
        token = self.get_token(budget)
        response, _ = send_with_retry(
            self.client, "GET", f"{self.base_url}/api/Tickets", idempotent=True, budget=budget, label=self.label,
            params={"third_party_id_string": ref}, headers=self._headers(token),
        )  # fmt: skip
        if not response.is_success:
            raise PsaError(error_reason("HaloPSA ticket search failed", response, secrets=self._secrets()))
        data = _json(response)
        if data is None:
            raise PsaError("HaloPSA ticket search returned an answer that is not JSON")
        items = _search_items(data)
        if items and not any("third_party_id_string" in _lower_keys(item) for item in items):
            # Without the field the match cannot be confirmed, so it is not trusted.
            raise PsaError("HaloPSA ticket search did not return the reference field")
        for item in items:
            fields = _lower_keys(item)
            if str(fields.get("third_party_id_string") or "").strip() == ref:
                found = _positive_id(fields.get("id"))
                if found:
                    return found
        return None

    def create_ticket(self, ticket: PsaTicket) -> PsaResult:
        budget = self._budget()
        url = f"{self.base_url}/api/Tickets"
        payload = [self.ticket_body(ticket)]
        refreshed = False

        def post(shared: RetryBudget) -> httpx.Response:
            nonlocal refreshed
            response = self._post(url, payload, shared)
            if response.status_code == 401 and not refreshed:
                # The resource server did not accept the token, so the ticket was not created. Refresh once.
                refreshed = True
                self._invalidate_token()
                response = self._post(url, payload, shared)
            return response

        outcome = create_with_lookup(
            post=post, lookup=lambda b: self.find_ticket(ticket.ref, b), budget=budget, label=self.label
        )
        if outcome.found_id:
            return self._result(outcome.found_id, outcome.attempts)
        response = outcome.response
        if response is None:
            raise PsaError("HaloPSA returned no answer", attempts=outcome.attempts)
        if response.is_success:
            created = parse_created_id(_json(response))
            return self._result(created or self._find_after_missing_id(ticket.ref, budget), outcome.attempts)
        raise self._create_error(response, outcome.attempts)

    def _post(self, url: str, payload: list[dict[str, Any]], budget: RetryBudget) -> httpx.Response:
        try:
            token = self.get_token(budget)
        except PsaError as exc:
            if not exc.ambiguous:
                raise
            # A token request that timed out is not an ambiguous create. The ticket was never sent.
            raise PsaError(
                "HaloPSA did not answer the sign in request, so the ticket was not sent and it is safe to retry",
                status_code=exc.status_code,
                attempts=exc.attempts,
                retryable=True,
                ambiguous=False,
            ) from exc
        response, _ = send_with_retry(
            self.client, "POST", url, idempotent=False, budget=budget, label=self.label,
            json=payload, headers={**self._headers(token), "Content-Type": "application/json"},
        )  # fmt: skip
        return response

    def _find_after_missing_id(self, ref: str, budget: RetryBudget) -> str:
        """Halo answered 2xx without an id. Look for the ticket, never send it again."""
        try:
            found = self.find_ticket(ref, budget.child())
        except PsaError:
            found = None
        if found:
            return found
        raise PsaError(
            "HaloPSA accepted the request but returned no ticket id. "
            "The ticket may have been created, so check HaloPSA before retrying",
            attempts=budget.attempts,
            ambiguous=True,
        )

    def _create_error(self, response: httpx.Response, attempts: int) -> PsaError:
        status = response.status_code
        if status == 401:
            prefix = "HaloPSA rejected the access token even after a refresh"
            return PsaError(
                f"{error_reason(prefix, response, secrets=self._secrets())}. {CREDENTIAL_HINT}",
                status_code=status, attempts=attempts,
            )  # fmt: skip
        if status == 403:
            prefix = "HaloPSA refused the request"
            return PsaError(
                f"{error_reason(prefix, response, secrets=self._secrets())}. "
                "Check the permissions of the Halo API application",
                status_code=status, attempts=attempts,
            )  # fmt: skip
        return PsaError(
            error_reason("HaloPSA rejected the ticket", response, secrets=self._secrets()),
            status_code=status,
            attempts=attempts,
            retryable=status in (429, 503),
        )

    def _result(self, ticket_id: str, attempts: int) -> PsaResult:
        logger.info("psa ticket created provider=halopsa id=%s attempts=%s", ticket_id, attempts)
        return PsaResult(
            provider=self.name, external_id=ticket_id, external_ref=None, url=self.ticket_url(ticket_id),
            attempts=attempts,
        )  # fmt: skip
