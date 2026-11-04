"""Retry classification, backoff, Retry-After, the deadline and the lookup before a re-sent create."""

import httpx
import pytest
from mock_psa import FakeClock

from triageops.psa.base import PsaError
from triageops.psa.retry import (
    RetryBudget,
    backoff_delay,
    classify_exception,
    classify_status,
    create_with_lookup,
    retry_after_seconds,
    send_with_retry,
)

URL = "https://psa.example/api/Tickets"
Step = int | type[Exception] | httpx.Response


class Scripted:
    """A transport that answers from a script of status codes, responses and exceptions."""

    def __init__(self, *steps: Step, clock: FakeClock | None = None, cost: float = 0.0) -> None:
        self.steps = list(steps)
        self.calls = 0
        self.clock, self.cost = clock, cost

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.clock:
            self.clock.advance(self.cost)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, httpx.Response):
            return step
        if isinstance(step, int):
            return httpx.Response(step, json={})
        raise step("simulated", request=request)


def send(
    script: Scripted, clock: FakeClock, *, idempotent: bool, attempts: int = 3, deadline: float = 20.0, **kwargs: object
) -> tuple[httpx.Response, int]:
    client = httpx.Client(transport=httpx.MockTransport(script))
    return send_with_retry(
        client, "POST", URL, idempotent=idempotent, max_attempts=attempts, backoff_seconds=0.5,
        deadline_seconds=deadline, sleep=clock.sleep, clock=clock, **kwargs,
    )  # fmt: skip


# Classification ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, "ok"), (201, "ok"), (204, "ok"), (429, "retryable"), (503, "retryable"), (500, "ambiguous"),
     (502, "ambiguous"), (504, "ambiguous"), (400, "permanent"), (401, "permanent"), (403, "permanent"),
     (404, "permanent"), (409, "permanent"), (501, "permanent")],
)  # fmt: skip
def test_status_classes(status: int, expected: str) -> None:
    assert classify_status(status) == expected


def test_permanent_markers_turn_a_500_permanent() -> None:
    markers = ("data violation",)
    assert classify_status(500, '{"errors":["Data violation: bad"]}', markers) == "permanent"
    assert classify_status(500, '{"errors":["Deadlock victim"]}', markers) == "ambiguous"
    assert classify_status(502, "data violation", markers) == "ambiguous", "only a 500 carries the markers"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(httpx.ConnectError, "retryable"), (httpx.ConnectTimeout, "retryable"), (httpx.PoolTimeout, "retryable"),
     (httpx.ReadTimeout, "ambiguous"), (httpx.WriteTimeout, "ambiguous"), (httpx.RemoteProtocolError, "ambiguous"),
     (httpx.ReadError, "ambiguous"), (httpx.UnsupportedProtocol, "permanent"), (httpx.LocalProtocolError, "permanent")],
)  # fmt: skip
def test_exception_classes(exc: type[Exception], expected: str) -> None:
    assert classify_exception(exc("x")) == expected


# Delays --------------------------------------------------------------------------

def test_backoff_doubles_and_is_capped_at_eight_seconds() -> None:
    assert [backoff_delay(n, 0.5) for n in range(1, 7)] == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]
    assert backoff_delay(1, 0.5, retry_after=30) == 8.0
    assert backoff_delay(1, 0.5, retry_after=2) == 2.0


def test_retry_after_in_seconds() -> None:
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "3"})) == 3.0
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "-5"})) == 0.0
    assert retry_after_seconds(httpx.Response(429)) is None
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"})) is None


def test_retry_after_as_an_http_date_uses_the_response_date() -> None:
    response = httpx.Response(
        503, headers={"Retry-After": "Wed, 04 Nov 2026 09:00:05 GMT", "Date": "Wed, 04 Nov 2026 09:00:00 GMT"}
    )
    assert retry_after_seconds(response) == 5.0
    past = httpx.Response(503, headers={"Retry-After": "Wed, 04 Nov 2026 08:00:00 GMT", "Date": "Wed, 04 Nov 2026 09:00:00 GMT"})
    assert retry_after_seconds(past) == 0.0


# send_with_retry -----------------------------------------------------------------

@pytest.mark.parametrize("status", [429, 503])
def test_always_retryable_statuses_are_retried_for_a_create(status: int) -> None:
    clock, script = FakeClock(), Scripted(status, status, 201)
    response, attempts = send(script, clock, idempotent=False)
    assert (response.status_code, attempts) == (201, 3)
    assert clock.sleeps == [0.5, 1.0]


def test_retry_after_seconds_is_honoured_and_capped() -> None:
    clock = FakeClock()
    script = Scripted(
        httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(429, headers={"Retry-After": "120"}), 200
    )
    response, attempts = send(script, clock, idempotent=False, deadline=60)
    assert (response.status_code, attempts) == (200, 3)
    assert clock.sleeps == [1.0, 8.0]


def test_retry_after_http_date_is_honoured() -> None:
    clock = FakeClock()
    script = Scripted(
        httpx.Response(503, headers={"Retry-After": "Wed, 04 Nov 2026 09:00:03 GMT", "Date": "Wed, 04 Nov 2026 09:00:00 GMT"}),
        200,
    )
    send(script, clock, idempotent=False)
    assert clock.sleeps == [3.0]


@pytest.mark.parametrize("status", [500, 502, 504])
def test_ambiguous_statuses_are_retried_when_idempotent(status: int) -> None:
    clock, script = FakeClock(), Scripted(status, 200)
    response, attempts = send(script, clock, idempotent=True)
    assert (response.status_code, attempts) == (200, 2)


@pytest.mark.parametrize("status", [500, 502, 504])
def test_ambiguous_statuses_are_not_resent_for_a_create(status: int) -> None:
    clock, script = FakeClock(), Scripted(status, 200)
    response, attempts = send(script, clock, idempotent=False)
    assert (response.status_code, attempts, script.calls) == (status, 1, 1)
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_statuses_are_never_retried(status: int) -> None:
    clock, script = FakeClock(), Scripted(status, 200)
    response, attempts = send(script, clock, idempotent=True)
    assert (response.status_code, attempts) == (status, 1)


def test_permanent_marker_stops_an_idempotent_retry() -> None:
    clock = FakeClock()
    script = Scripted(httpx.Response(500, json={"errors": ["IntegrationCode is invalid."]}), 200)
    response, attempts = send(script, clock, idempotent=True, permanent_markers=("integrationcode is invalid",))
    assert (response.status_code, attempts) == (500, 1)


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout])
def test_connection_failures_are_retried_for_a_create(exc: type[Exception]) -> None:
    clock, script = FakeClock(), Scripted(exc, 201)
    response, attempts = send(script, clock, idempotent=False)
    assert (response.status_code, attempts) == (201, 2)


def test_connection_failures_raise_a_retryable_error_when_attempts_run_out() -> None:
    clock, script = FakeClock(), Scripted(httpx.ConnectError)
    with pytest.raises(PsaError) as caught:
        send(script, clock, idempotent=False, label="HaloPSA")
    error = caught.value
    assert (error.attempts, error.retryable, error.ambiguous, script.calls) == (3, True, False, 3)
    assert error.message == "HaloPSA could not be reached (ConnectError)"


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_ambiguous_transport_failures_are_not_resent_for_a_create(exc: type[Exception]) -> None:
    clock, script = FakeClock(), Scripted(exc, 201)
    with pytest.raises(PsaError) as caught:
        send(script, clock, idempotent=False)
    assert caught.value.ambiguous and caught.value.attempts == 1
    assert script.calls == 1


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_ambiguous_transport_failures_are_retried_when_idempotent(exc: type[Exception]) -> None:
    clock, script = FakeClock(), Scripted(exc, 200)
    response, attempts = send(script, clock, idempotent=True)
    assert (response.status_code, attempts) == (200, 2)


def test_permanent_transport_failure_is_not_retried() -> None:
    clock, script = FakeClock(), Scripted(httpx.UnsupportedProtocol, 200)
    with pytest.raises(PsaError) as caught:
        send(script, clock, idempotent=True)
    assert (caught.value.retryable, caught.value.attempts) == (False, 1)


def test_exhausted_retries_return_the_last_response() -> None:
    clock, script = FakeClock(), Scripted(503)
    response, attempts = send(script, clock, idempotent=False, attempts=2)
    assert (response.status_code, attempts) == (503, 2)
    assert clock.sleeps == [0.5]


def test_deadline_stops_retries_before_the_next_wait_would_pass_it() -> None:
    clock = FakeClock()
    script = Scripted(503, clock=clock, cost=4.0)  # each attempt takes 4 seconds
    response, attempts = send(script, clock, idempotent=False, attempts=5, deadline=10.0)
    # Attempt 1 ends at 4 s and attempt 2 at 8.5 s. The wait of 1 s still fits, but after attempt 3
    # (13.5 s) the deadline has passed, so no further wait or attempt is made.
    assert response.status_code == 503
    assert attempts == 3
    assert clock.sleeps == [0.5, 1.0]


def test_long_retry_after_is_not_honoured_past_the_deadline() -> None:
    clock = FakeClock()
    script = Scripted(httpx.Response(429, headers={"Retry-After": "8"}), 200)
    response, attempts = send(script, clock, idempotent=False, deadline=5.0)
    assert (response.status_code, attempts) == (429, 1)
    assert clock.sleeps == []


def test_a_spent_budget_sends_nothing() -> None:
    clock = FakeClock()
    budget = RetryBudget(3, 0.5, 1.0, clock.sleep, clock)
    clock.advance(2)
    script = Scripted(200)
    with pytest.raises(PsaError) as caught:
        send_with_retry(httpx.Client(transport=httpx.MockTransport(script)), "GET", URL, idempotent=True, budget=budget)
    assert caught.value.retryable and script.calls == 0


def test_attempt_timeout_is_shortened_to_the_remaining_budget() -> None:
    seen: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200)

    clock = FakeClock()
    budget = RetryBudget(3, 0.5, 5.0, clock.sleep, clock)
    clock.advance(2)
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=8.0)
    send_with_retry(client, "GET", URL, idempotent=True, budget=budget)
    assert seen[0]["read"] == pytest.approx(3.0)


# create_with_lookup --------------------------------------------------------------

def run_create(
    responses: list[Step], lookups: list[str | None | Exception], *, attempts: int = 3
) -> tuple[object, list[int], FakeClock]:
    clock = FakeClock()
    script = Scripted(*responses)
    client = httpx.Client(transport=httpx.MockTransport(script))
    budget = RetryBudget(attempts, 0.5, 20.0, clock.sleep, clock)
    lookup_calls: list[int] = []

    def post(shared: RetryBudget) -> httpx.Response:
        response, _ = send_with_retry(client, "POST", URL, idempotent=False, budget=shared, label="the PSA")
        return response

    def lookup(shared: RetryBudget) -> str | None:
        lookup_calls.append(script.calls)
        result = lookups.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    outcome = create_with_lookup(post=post, lookup=lookup, budget=budget, label="the PSA")
    return outcome, lookup_calls, clock


def test_create_success_needs_no_lookup() -> None:
    outcome, lookups, _ = run_create([201], [])
    assert outcome.response.status_code == 201 and outcome.found_id is None and outcome.attempts == 1
    assert lookups == []


def test_ambiguous_create_found_by_lookup_is_a_success() -> None:
    outcome, lookups, clock = run_create([500, 201], ["777"])
    assert (outcome.found_id, outcome.response, outcome.attempts) == ("777", None, 1)
    assert lookups == [1], "the lookup runs after the first send and nothing is re-sent"
    assert clock.sleeps == []


def test_ambiguous_create_not_found_is_resent_after_a_backoff() -> None:
    outcome, lookups, clock = run_create([502, 201], [None])
    assert outcome.response.status_code == 201 and outcome.attempts == 2
    assert clock.sleeps == [0.5]


def test_read_timeout_create_not_found_is_resent() -> None:
    outcome, lookups, _ = run_create([httpx.ReadTimeout, 201], [None])
    assert outcome.response.status_code == 201 and outcome.attempts == 2


def test_ambiguous_create_not_found_with_no_attempts_left_fails_as_retryable() -> None:
    with pytest.raises(PsaError) as caught:
        run_create([500], [None, None, None], attempts=2)
    error = caught.value
    assert (error.attempts, error.retryable, error.status_code) == (2, True, 500)
    assert "was not found" in error.message


def test_failed_lookup_stops_and_says_the_ticket_may_exist() -> None:
    with pytest.raises(PsaError) as caught:
        run_create([504, 201], [PsaError("search failed")])
    error = caught.value
    assert error.ambiguous and not error.retryable and error.attempts == 1
    assert "may already exist" in error.message and "check there before retrying" in error.message


def test_non_ambiguous_errors_are_returned_without_a_lookup() -> None:
    outcome, lookups, _ = run_create([400], [])
    assert outcome.response.status_code == 400 and lookups == []


def test_transport_failure_before_sending_is_raised_without_a_lookup() -> None:
    with pytest.raises(PsaError) as caught:
        run_create([httpx.ConnectError], [], attempts=1)
    assert caught.value.retryable and not caught.value.ambiguous

