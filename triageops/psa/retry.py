"""Retry, backoff and the deadline for PSA calls.

Every outcome falls into one of four classes:

* ``ok``: a 2xx answer.
* ``retryable``: the PSA did not process the request (connect error, connect
  timeout, pool timeout, HTTP 429 and 503). It is always safe to send again.
* ``ambiguous``: the request may have been processed (read or write timeout,
  a dropped connection, HTTP 500, 502 and 504). A read is sent again. A create
  is NOT sent again blindly, because that could open a duplicate PSA ticket.
  ``create_with_lookup`` looks the ticket up by our reference first.
* ``permanent``: any other answer, such as a 4xx or an Autotask 500 whose text
  marks a validation or configuration error. Sending it again cannot help.

The wait before attempt ``n + 1`` is ``min(8, backoff * 2 ** (n - 1))`` seconds,
or the ``Retry-After`` header (seconds or an HTTP date) capped at 8 seconds.
A ``RetryBudget`` counts attempts and stops before the next wait would pass the
total deadline, so the serverless function stays well inside its time limit.
``sleep`` and ``clock`` are injectable so tests never wait.
"""

import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

from .base import PsaError

logger = logging.getLogger(__name__)

MAX_DELAY_SECONDS = 8.0

Outcome = Literal["ok", "retryable", "ambiguous", "permanent"]

ALWAYS_RETRY_STATUSES = frozenset({429, 503})
AMBIGUOUS_STATUSES = frozenset({500, 502, 504})


# Classification -------------------------------------------------------------------

def classify_status(status_code: int, text: str = "", permanent_markers: Sequence[str] = ()) -> Outcome:
    if 200 <= status_code < 300:
        return "ok"
    if status_code in ALWAYS_RETRY_STATUSES:
        return "retryable"
    if status_code in AMBIGUOUS_STATUSES:
        lowered = text.lower()
        if status_code == 500 and any(marker.lower() in lowered for marker in permanent_markers):
            return "permanent"
        return "ambiguous"
    return "permanent"


def classify_response(response: httpx.Response, permanent_markers: Sequence[str] = ()) -> Outcome:
    text = response.text[:4000] if permanent_markers and response.content else ""
    return classify_status(response.status_code, text, permanent_markers)


def classify_exception(exc: Exception) -> Outcome:
    """Connection failures happen before anything is sent. A timeout or a drop after sending is ambiguous."""
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return "retryable"
    if isinstance(exc, httpx.TimeoutException | httpx.RemoteProtocolError | httpx.ReadError | httpx.WriteError):
        return "ambiguous"
    return "permanent"


def retry_after_seconds(response: httpx.Response) -> float | None:
    """``Retry-After`` as seconds from now. An HTTP date is measured against the response ``Date`` header."""
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    reference = datetime.now(UTC)
    date_header = response.headers.get("date")
    if date_header:
        try:
            reference = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            pass
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(0.0, (when - reference).total_seconds())


def backoff_delay(attempt: int, backoff_seconds: float, retry_after: float | None = None) -> float:
    """The wait after attempt number ``attempt`` (counted from 1)."""
    if retry_after is not None:
        return min(MAX_DELAY_SECONDS, retry_after)
    return min(MAX_DELAY_SECONDS, backoff_seconds * 2 ** max(0, attempt - 1))


# Budget ---------------------------------------------------------------------------

@dataclass
class RetryBudget:
    """Attempts and the deadline shared by every request of one PSA operation."""

    max_attempts: int
    backoff_seconds: float
    deadline_seconds: float
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    attempts: int = 0
    started: float = field(default=-1.0)

    def __post_init__(self) -> None:
        if self.started < 0:
            self.started = self.clock()

    def child(self) -> "RetryBudget":
        """A budget with its own attempt count that shares this deadline, used for lookups and tokens."""
        return RetryBudget(
            self.max_attempts, self.backoff_seconds, self.deadline_seconds, self.sleep, self.clock, 0, self.started
        )

    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining(self) -> float:
        return self.deadline_seconds - self.elapsed()

    def can_retry(self, delay: float) -> bool:
        return self.attempts < self.max_attempts and self.elapsed() + delay < self.deadline_seconds

    def wait(self, delay: float) -> None:
        if delay > 0:
            self.sleep(delay)


# Sending --------------------------------------------------------------------------

def _timeout_for(client: httpx.Client, remaining: float) -> httpx.Timeout:
    """The client timeout, shortened so one attempt cannot run past the deadline."""
    configured = client.timeout.read if client.timeout.read is not None else remaining
    return httpx.Timeout(max(0.1, min(configured, remaining)))


def send_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    idempotent: bool,
    max_attempts: int = 3,
    backoff_seconds: float = 0.5,
    deadline_seconds: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    budget: RetryBudget | None = None,
    permanent_markers: Sequence[str] = (),
    label: str = "the PSA",
    **request_kwargs: Any,
) -> tuple[httpx.Response, int]:
    """Send one request, retrying what is safe to retry. Returns the final response and the attempt count.

    The caller checks the final status. A transport failure that is not retried
    raises ``PsaError`` (``ambiguous`` when the request may have been processed).
    When ``budget`` is given it replaces the numeric limits, so several calls
    share one attempt count and one deadline. The first send of a call always
    goes out while the deadline has not passed.
    """
    budget = budget or RetryBudget(max_attempts, backoff_seconds, deadline_seconds, sleep, clock)
    if budget.remaining() <= 0:
        raise PsaError(
            f"The time budget for {label} was used up before the request was sent",
            attempts=budget.attempts,
            retryable=True,
        )
    while True:
        budget.attempts += 1
        try:
            response = client.request(
                method, url, timeout=_timeout_for(client, budget.remaining()), **request_kwargs
            )
        except httpx.HTTPError as exc:
            outcome = classify_exception(exc)
            name = type(exc).__name__
            if outcome == "retryable" or (outcome == "ambiguous" and idempotent):
                delay = backoff_delay(budget.attempts, budget.backoff_seconds)
                if budget.can_retry(delay):
                    logger.info("psa request retry method=%s error=%s attempt=%s", method, name, budget.attempts)
                    budget.wait(delay)
                    continue
            raise PsaError(
                _transport_message(label, name, outcome),
                attempts=budget.attempts,
                retryable=outcome != "permanent",
                ambiguous=outcome == "ambiguous",
            ) from exc

        outcome = classify_response(response, permanent_markers)
        if outcome == "retryable" or (outcome == "ambiguous" and idempotent):
            delay = backoff_delay(budget.attempts, budget.backoff_seconds, retry_after_seconds(response))
            if budget.can_retry(delay):
                logger.info(
                    "psa request retry method=%s status=%s attempt=%s", method, response.status_code, budget.attempts
                )
                budget.wait(delay)
                continue
        return response, budget.attempts


def sentence(text: str) -> str:
    """Capitalise the first letter, so a label such as "the PSA" can start a message."""
    return text[:1].upper() + text[1:]


def _transport_message(label: str, name: str, outcome: Outcome) -> str:
    if outcome == "ambiguous":
        return f"The connection to {label} failed after the request was sent ({name})"
    if outcome == "retryable":
        return sentence(f"{label} could not be reached ({name})")
    return f"The request to {label} failed ({name})"


# Creating without duplicates ----------------------------------------------------

@dataclass(frozen=True)
class CreateOutcome:
    """Either the final create response, or the id found by the lookup after an ambiguous create."""

    response: httpx.Response | None
    found_id: str | None
    attempts: int


def create_with_lookup(
    *,
    post: Callable[[RetryBudget], httpx.Response],
    lookup: Callable[[RetryBudget], str | None],
    budget: RetryBudget,
    label: str,
    permanent_markers: Sequence[str] = (),
) -> CreateOutcome:
    """Create a ticket so that an ambiguous answer never opens a duplicate.

    ``post`` sends the create once (with the always safe retries) and returns
    the response. When the outcome is ambiguous, ``lookup`` searches the PSA
    for our reference. Found means the first create worked. Not found means
    back off and send again within the budget. A failed lookup stops at once
    with a message saying the ticket may already exist.
    """
    while True:
        try:
            response = post(budget)
        except PsaError as exc:
            if not exc.ambiguous:
                raise
            response, cause, status = None, exc.message, None
        else:
            if classify_response(response, permanent_markers) != "ambiguous":
                return CreateOutcome(response, None, budget.attempts)
            cause = sentence(f"{label} answered HTTP {response.status_code}")
            status = response.status_code

        found = _lookup_or_fail(lookup, budget, label, cause, status)
        if found:
            logger.info("psa create confirmed by lookup attempts=%s", budget.attempts)
            return CreateOutcome(None, found, budget.attempts)

        delay = backoff_delay(budget.attempts, budget.backoff_seconds)
        if not budget.can_retry(delay):
            raise PsaError(
                f"{cause}. The ticket was not found in {label}, and no attempts or time were left to send it again",
                status_code=status,
                attempts=budget.attempts,
                retryable=True,
            )
        budget.wait(delay)


def _lookup_or_fail(
    lookup: Callable[[RetryBudget], str | None], budget: RetryBudget, label: str, cause: str, status: int | None
) -> str | None:
    try:
        return lookup(budget.child())
    except Exception as exc:
        logger.warning("psa lookup after an ambiguous create failed error=%s", type(exc).__name__)
        raise PsaError(
            f"{cause}, and the check for an existing ticket also failed. "
            f"The ticket may already exist in {label}, so check there before retrying",
            status_code=status,
            attempts=budget.attempts,
            retryable=False,
            ambiguous=True,
        ) from exc
