"""A stateful in-memory PSA server for ``httpx.MockTransport``. No sockets.

``MockPsaServer`` answers the HaloPSA token, create and search endpoints and the
Autotask zone, create and query endpoints, with the validation and answer
shapes from the verified vendor specs. It records every request and supports
a failure script:

    server.fail_next(503, times=2, route="halo_create", retry_after="1")
    server.fail_next(500, route="autotask_create", commit=True)  # created, then a 500
    server.fail_next_transport(httpx.ReadTimeout, route="halo_create", commit=True)
    server.expire_tokens()

``commit=True`` processes the request first and then fails, which is the
ambiguous case where the PSA created the ticket but the answer was lost.
Use it as ``httpx.Client(transport=httpx.MockTransport(server.handle))``.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qs

import httpx

from triageops.psa.config import PsaSettings

HALO_CLIENT_ID = "halo-client-id"
HALO_CLIENT_SECRET = "halo-client-secret-value"
AUTOTASK_USERNAME = "api.user@msp.example"
AUTOTASK_SECRET = "autotask-secret-value"
AUTOTASK_INTEGRATION_CODE = "INTEGRATIONCODE123"
AUTOTASK_ZONE = "https://webservices15.autotask.net/atservicesrest/"

# A triaged ticket row as ``store`` returns it. Text values are already masked.
TICKET_ROW: dict[str, Any] = {
    "id": "6f1d2c3b-0000-4000-8000-000000000001",
    "ref": "TKT-1104-0001",
    "created_at": datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
    "subject": "Disk space low on SRV-FS01",
    "body": "Free space on C: is 3 percent. Contact [REDACTED:email].",
    "summary": "File server disk nearly full.",
    "priority": "P2",
    "category": "Hardware",
    "team": "Infrastructure L2",
    "status": "triaged",
    "customer": "Harbour Dental Group",
    "requester": None,
    "sla_due_at": datetime(2026, 11, 4, 13, 0, tzinfo=UTC),
    "next_steps": ["Clear temporary files", "Extend the volume"],
    "escalation_reason": None,
    "injection_suspected": False,
    "suggested_reply": "Hello, this draft must never reach the PSA.",
}

Route = Literal[
    "halo_token", "halo_create", "halo_search", "autotask_zone", "autotask_create", "autotask_query", "unknown"
]
HaloShape = Literal["object", "list", "wrapper", "empty"]


@dataclass
class _Failure:
    route: str | None
    times: int
    commit: bool
    status: int | None = None
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    exception: type[Exception] | None = None


def _json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


class MockPsaServer:
    def __init__(
        self,
        *,
        halo_tenant: str | None = None,
        halo_shape: HaloShape = "object",
        token_expires_in: int = 3600,
        halo_search_has_ref_field: bool = True,
        autotask_zone_url: str = AUTOTASK_ZONE,
        autotask_known_users: tuple[str, ...] = (AUTOTASK_USERNAME,),
        autotask_priorities: tuple[int, ...] = (1, 2, 3, 4),
    ) -> None:
        self.halo_tenant = halo_tenant
        self.halo_shape = halo_shape
        self.token_expires_in = token_expires_in
        self.halo_search_has_ref_field = halo_search_has_ref_field
        self.autotask_zone_url = autotask_zone_url
        self.autotask_known_users = autotask_known_users
        self.autotask_priorities = autotask_priorities

        self.requests: list[httpx.Request] = []
        self.routes: list[str] = []
        self.halo_tickets: dict[int, dict[str, Any]] = {}
        self.autotask_tickets: dict[int, dict[str, Any]] = {}
        self.tokens_issued: list[str] = []
        self._valid_tokens: set[str] = set()
        self._next_halo_id = 54321
        self._next_autotask_id = 29683664
        self._failures: list[_Failure] = []

    # Failure script -----------------------------------------------------------------

    def fail_next(
        self,
        status: int,
        *,
        times: int = 1,
        route: Route | None = None,
        retry_after: str | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        commit: bool = False,
    ) -> None:
        """The next ``times`` requests to ``route`` (any route when None) answer ``status``."""
        extra = dict(headers or {})
        if retry_after is not None:
            extra["Retry-After"] = retry_after
        self._failures.append(_Failure(route, times, commit, status=status, body=body, headers=extra))

    def fail_next_transport(
        self, exception: type[Exception], *, times: int = 1, route: Route | None = None, commit: bool = False
    ) -> None:
        """The next ``times`` requests to ``route`` raise ``exception`` as the transport would."""
        self._failures.append(_Failure(route, times, commit, exception=exception))

    def expire_tokens(self) -> None:
        """Every issued HaloPSA token stops working, so the next resource call answers 401."""
        self._valid_tokens.clear()

    def _take_failure(self, route: str) -> _Failure | None:
        for failure in self._failures:
            if failure.route in (None, route):
                failure.times -= 1
                if failure.times <= 0:
                    self._failures.remove(failure)
                return failure
        return None

    # Inspection ---------------------------------------------------------------------

    def calls(self, route: Route) -> list[httpx.Request]:
        return [request for request, name in zip(self.requests, self.routes, strict=True) if name == route]

    def body(self, request: httpx.Request) -> Any:
        return json.loads(request.content)

    # Dispatch -----------------------------------------------------------------------

    @staticmethod
    def route_of(request: httpx.Request) -> str:
        path, method = request.url.path.lower().rstrip("/"), request.method
        if method == "POST" and path.endswith("/auth/token"):
            return "halo_token"
        if path.endswith("/api/tickets"):
            return {"POST": "halo_create", "GET": "halo_search"}.get(method, "unknown")
        if method == "GET" and path.endswith("/atservicesrest/v1.0/zoneinformation"):
            return "autotask_zone"
        if method == "POST" and path.endswith("/atservicesrest/v1.0/tickets"):
            return "autotask_create"
        if method == "POST" and path.endswith("/atservicesrest/v1.0/tickets/query"):
            return "autotask_query"
        return "unknown"

    def handle(self, request: httpx.Request) -> httpx.Response:
        route = self.route_of(request)
        self.requests.append(request)
        self.routes.append(route)
        failure = self._take_failure(route)
        if failure and not failure.commit:
            return self._fail(failure, request)
        response = self._dispatch(route, request)
        return self._fail(failure, request) if failure else response

    def _fail(self, failure: _Failure, request: httpx.Request) -> httpx.Response:
        if failure.exception is not None:
            raise failure.exception("simulated transport failure", request=request)
        status = failure.status or 500
        if isinstance(failure.body, str):
            return httpx.Response(status, text=failure.body, headers=failure.headers)
        body = failure.body if failure.body is not None else {"errors": [f"Simulated HTTP {status}"]}
        return _json_response(status, body, failure.headers)

    def _dispatch(self, route: str, request: httpx.Request) -> httpx.Response:
        handler = {
            "halo_token": self._halo_token,
            "halo_create": self._halo_create,
            "halo_search": self._halo_search,
            "autotask_zone": self._autotask_zone,
            "autotask_create": self._autotask_create,
            "autotask_query": self._autotask_query,
        }.get(route)
        if handler is None:
            return _json_response(404, {"error": "not found"})
        return handler(request)

    # HaloPSA ------------------------------------------------------------------------

    def _halo_token(self, request: httpx.Request) -> httpx.Response:
        if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
            return _json_response(400, {"error": "invalid_request"})
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") != "client_credentials":
            return _json_response(400, {"error": "unsupported_grant_type"})
        if self.halo_tenant and request.url.params.get("tenant") != self.halo_tenant:
            # Halo answers a wrong or missing tenant with an HTML error page.
            return httpx.Response(500, text="<html><body>Server Error</body></html>",
                                  headers={"content-type": "text/html"})  # fmt: skip
        if form.get("client_id") != HALO_CLIENT_ID or form.get("client_secret") != HALO_CLIENT_SECRET:
            return _json_response(401, {"error": "invalid_client", "error_description": "Invalid client credentials"})
        token = f"halo-token-{len(self.tokens_issued) + 1}"
        self.tokens_issued.append(token)
        self._valid_tokens.add(token)
        return _json_response(
            200, {"token_type": "Bearer", "access_token": token, "expires_in": self.token_expires_in, "scope": "all"}
        )

    def _halo_authorised(self, request: httpx.Request) -> bool:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return scheme == "Bearer" and token in self._valid_tokens

    def _halo_create(self, request: httpx.Request) -> httpx.Response:
        if not self._halo_authorised(request):
            return httpx.Response(401, text="")
        try:
            body = json.loads(request.content)
        except ValueError:
            return _json_response(400, {"error": "The body must be JSON"})
        if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
            return _json_response(400, {"[0]": "The body must be an array holding one ticket"})
        fields = body[0]
        for name in ("summary", "tickettype_id", "third_party_id_string"):
            if fields.get(name) in (None, ""):
                return _json_response(400, {f"[0].{name}": f"{name} is required"})
        if not isinstance(fields["tickettype_id"], int) or fields.get("id"):
            return _json_response(400, {"[0].tickettype_id": "tickettype_id must be an integer"})
        ticket_id = self._next_halo_id
        self._next_halo_id += 1
        stored = {**fields, "id": ticket_id}
        self.halo_tickets[ticket_id] = stored
        if self.halo_shape == "object":
            return _json_response(201, stored)
        if self.halo_shape == "list":
            return _json_response(200, [{"id": ticket_id}])
        if self.halo_shape == "wrapper":
            return _json_response(200, {"ticket": {"id": ticket_id}, "success": True})
        return _json_response(200, {})

    def _halo_search(self, request: httpx.Request) -> httpx.Response:
        if not self._halo_authorised(request):
            return httpx.Response(401, text="")
        wanted = request.url.params.get("third_party_id_string")
        found = [t for t in self.halo_tickets.values() if wanted is None or t.get("third_party_id_string") == wanted]
        rows = []
        for ticket in found:
            row = {"id": ticket["id"], "summary": ticket["summary"]}
            if self.halo_search_has_ref_field:
                row["third_party_id_string"] = ticket["third_party_id_string"]
            rows.append(row)
        return _json_response(200, {"record_count": len(rows), "tickets": rows})

    # Autotask -----------------------------------------------------------------------

    def _autotask_zone(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("user") not in self.autotask_known_users:
            return _json_response(500, {"errors": ["Zone information could not be determined"]})
        return _json_response(
            200, {"zoneName": "Test", "url": self.autotask_zone_url, "webUrl": "https://ww15.autotask.net/", "ci": 1}
        )

    def _autotask_authorised(self, request: httpx.Request) -> bool:
        headers = request.headers  # httpx headers are case insensitive, as Autotask is.
        return (
            headers.get("username") == AUTOTASK_USERNAME
            and headers.get("secret") == AUTOTASK_SECRET
            and headers.get("apiintegrationcode") == AUTOTASK_INTEGRATION_CODE
        )

    def _autotask_create(self, request: httpx.Request) -> httpx.Response:
        if not self._autotask_authorised(request):
            return httpx.Response(401, text="")
        try:
            body = json.loads(request.content)
        except ValueError:
            return _json_response(500, {"errors": ["The request body is not valid JSON"]})
        if not isinstance(body, dict) or "id" in body:
            return _json_response(500, {"errors": ["The request body must be one ticket object without an id"]})
        for name in ("companyID", "status", "priority"):
            if not isinstance(body.get(name), int) or isinstance(body.get(name), bool):
                return _json_response(500, {"errors": [f"{name} is required"]})
        title = body.get("title")
        if not isinstance(title, str) or not title or len(title) > 255:
            return _json_response(500, {"errors": ["Title is required and must be at most 255 characters"]})
        if body["priority"] not in self.autotask_priorities:
            return _json_response(500, {"errors": [f"Value {body['priority']} does not exist for priority"]})
        due = body.get("dueDateTime")
        try:
            datetime.fromisoformat(str(due).replace("Z", "+00:00"))
        except ValueError:
            return _json_response(500, {"errors": ["DueDateTime is required"]})
        ticket_id = self._next_autotask_id
        self._next_autotask_id += 1
        self.autotask_tickets[ticket_id] = {**body, "id": ticket_id}
        return _json_response(200, {"itemId": ticket_id})

    def _autotask_query(self, request: httpx.Request) -> httpx.Response:
        if not self._autotask_authorised(request):
            return httpx.Response(401, text="")
        query = json.loads(request.content)
        filters = query.get("filter") or []
        items = list(self.autotask_tickets.values())
        for condition in filters:
            if condition.get("op") != "eq":
                return _json_response(500, {"errors": ["Only eq is supported by the mock"]})
            items = [t for t in items if str(t.get(condition["field"])) == str(condition["value"])]
        items = items[: int(query.get("maxRecords") or 500)]
        return _json_response(200, {"items": items, "pageDetails": {"count": len(items), "nextPageUrl": None}})


# Test helpers -----------------------------------------------------------------------

class FakeClock:
    """A monotonic clock and a sleep that records delays and moves the clock instead of waiting."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


HALO_ENV = {
    "PSA_PROVIDER": "halopsa",
    "HALOPSA_BASE_URL": "https://acme.halopsa.com",
    "HALOPSA_CLIENT_ID": HALO_CLIENT_ID,
    "HALOPSA_CLIENT_SECRET": HALO_CLIENT_SECRET,
    "HALOPSA_TICKET_TYPE_ID": "31",
}

AUTOTASK_ENV = {
    "PSA_PROVIDER": "autotask",
    "AUTOTASK_USERNAME": AUTOTASK_USERNAME,
    "AUTOTASK_SECRET": AUTOTASK_SECRET,
    "AUTOTASK_INTEGRATION_CODE": AUTOTASK_INTEGRATION_CODE,
    "AUTOTASK_COMPANY_ID": "0",
}


def halo_settings(**env: str) -> PsaSettings:
    return PsaSettings.from_env({**HALO_ENV, **env})


def autotask_settings(**env: str) -> PsaSettings:
    return PsaSettings.from_env({**AUTOTASK_ENV, **env})


def client_for(server: MockPsaServer) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server.handle), timeout=8.0)
