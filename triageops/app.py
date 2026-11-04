"""The FastAPI application. Every route lives under ``/api``.

Route handlers are synchronous because psycopg is used in its sync form, and
FastAPI runs them in a thread pool so blocking I/O never stalls the event loop.
Signed webhooks read the raw body through an async dependency, because the
HMAC must be computed over exactly the bytes that were signed. Clients only
ever see short generic error messages. Details go to the server log.
"""

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import anthropic
import psycopg
from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import agent, channels, db, kb_seed, mcp, rag, store, triage
from .config import VERSION, settings
from .db import Connection, DatabaseUnavailable
from .rag import EmbeddingError
from .rmm import routes as rmm_routes
from .schemas import ChatIn, DemoTicketIn, InboundTicketIn, PostmarkInboundIn
from .security import (
    MCP_CORS_HEADERS,
    SECURITY_HEADERS,
    SignatureError,
    SlidingWindowLimiter,
    bearer_token_matches,
    client_ip,
    hash_client,
    tokens_match,
    verify_meta_signature,
    verify_timestamped_signature,
)
from .taxonomy import CHANNELS, PRIORITIES, STATUSES

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
# httpx logs every request URL at INFO. Slack, Discord and Teams webhook URLs are secrets, so keep them out.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("triageops")

MAX_BODY_BYTES = 64 * 1024
MCP_PATH = "/api/mcp"
GENERIC_ERROR = "Something went wrong. Please try again."
UNAVAILABLE_ERROR = "The service is temporarily unavailable. Please try again shortly."

app = FastAPI(
    title="TriageOps API",
    version=VERSION,
    # The Swagger UI needs inline scripts, which the CSP forbids. The schema stays available.
    docs_url=None,
    redoc_url=None,
    openapi_url="/api/openapi.json",
)
# RMM alert intake lives in its own module, see triageops/rmm/routes.py.
app.include_router(rmm_routes.router)
chat_limiter = SlidingWindowLimiter(limit=settings.chat_hourly_limit, window_seconds=3600)

Model = TypeVar("Model", bound=BaseModel)


class ApiError(Exception):
    """An expected error with a client safe message."""

    def __init__(self, status_code: int, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.headers = headers


# Middleware and error handling -------------------------------------------------------

@app.middleware("http")
async def harden_responses(request: Request, call_next: Any) -> Response:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        response: Response = JSONResponse({"error": "The request body is too large."}, status_code=413)
    else:
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled error path=%s", request.url.path)
            response = JSONResponse({"error": GENERIC_ERROR}, status_code=500)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.path == MCP_PATH:
        response.headers.update(MCP_CORS_HEADERS)
    return response


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [
        {
            "field": ".".join(str(part) for part in error["loc"] if part not in ("body", "query", "path")) or "body",
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    first = fields[0] if fields else {"field": "body", "message": "Invalid value"}
    message = f"Check the {first['field']} field. {first['message']}."
    return JSONResponse({"error": message, "fields": fields}, status_code=422)


@app.exception_handler(DatabaseUnavailable)
@app.exception_handler(psycopg.Error)
async def _database_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("database error path=%s error=%s", request.url.path, type(exc).__name__)
    return JSONResponse({"error": UNAVAILABLE_ERROR}, status_code=503)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)


# Request helpers -------------------------------------------------------------------

async def raw_body(request: Request) -> bytes:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(413, "The request body is too large.")
    return body


def _parse(model: type[Model], raw: bytes) -> Model:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ApiError(400, "The request body must be valid JSON.") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _client_hash(request: Request) -> str:
    peer = request.client.host if request.client else None
    return hash_client(client_ip(request.headers, peer), settings.ip_hash_salt)


def _verify_signed_webhook(request: Request, raw: bytes) -> None:
    try:
        verify_timestamped_signature(
            settings.webhook_secret,
            request.headers.get("x-triageops-timestamp"),
            request.headers.get("x-triageops-signature"),
            raw,
        )
    except SignatureError as exc:
        logger.warning("signature rejected path=%s reason=%s", request.url.path, exc)
        raise ApiError(401, "Invalid or expired signature.") from exc


def _require_bearer(request: Request, expected: str, realm: str) -> None:
    if not bearer_token_matches(request.headers.get("authorization"), expected):
        if not expected:
            logger.warning("%s token is not configured, rejecting every request", realm)
        raise ApiError(401, "Unauthorised.", headers={"WWW-Authenticate": f'Bearer realm="{realm}"'})


def _optional_choice(value: str | None, allowed: tuple[str, ...], name: str) -> str | None:
    """Empty means no filter. The dashboard sends empty strings for unset filters."""
    if not value:
        return None
    if value not in allowed:
        raise ApiError(422, f"Unknown {name} filter.")
    return value


def _bundle_json(bundle: triage.TicketBundle) -> dict[str, Any]:
    return {"ticket": store.to_json(bundle.ticket), "events": [store.to_json(event) for event in bundle.events]}


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((midnight - now).total_seconds()), 1)


def _enforce_demo_limits(conn: Connection, client_hash: str) -> None:
    if store.count_recent_tickets_from(conn, client_hash) >= settings.demo_hourly_limit:
        raise ApiError(
            429,
            f"The demo allows {settings.demo_hourly_limit} tickets an hour from one connection. Please try again later.",
            headers={"Retry-After": "3600"},
        )
    if store.count_demo_tickets_today(conn) >= settings.demo_daily_limit:
        raise ApiError(
            429,
            "The demo has reached its daily ticket limit. Please try again tomorrow.",
            headers={"Retry-After": str(_seconds_until_utc_midnight())},
        )


def _kb_count_seeding_if_empty(conn: Connection) -> int:
    count = store.kb_count(conn)
    if count == 0 and rag.is_configured():
        try:
            inserted = kb_seed.seed_kb(conn)
        except EmbeddingError as exc:
            logger.warning("knowledge base seeding failed: %s", exc)
            return 0
        logger.info("seeded %d knowledge base articles", inserted)
        count = store.kb_count(conn)
    return count


# Read endpoints --------------------------------------------------------------------

@app.get("/api/health")
def health() -> JSONResponse:
    db_ok, kb_articles = True, 0
    try:
        with db.connect() as conn:
            kb_articles = _kb_count_seeding_if_empty(conn)
    except (DatabaseUnavailable, psycopg.Error) as exc:
        logger.warning("health check database error=%s", type(exc).__name__)
        db_ok = False
    body: dict[str, Any] = {
        "ok": db_ok,
        "version": VERSION,
        "db": db_ok,
        "ai": bool(settings.anthropic_api_key),
        "kb_articles": kb_articles,
        "region": settings.region,
    }
    if not db_ok:
        body["error"] = "The database is unreachable."
    return JSONResponse(body, status_code=200 if db_ok else 503)


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    with db.connect() as conn:
        return store.queue_stats(conn)


@app.get("/api/tickets")
def list_tickets(
    status: str | None = Query(default=None, max_length=20),
    priority: str | None = Query(default=None, max_length=5),
    channel: str | None = Query(default=None, max_length=20),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    filters = {
        "status": _optional_choice(status, STATUSES, "status"),
        "priority": _optional_choice(priority, PRIORITIES, "priority"),
        "channel": _optional_choice(channel, CHANNELS, "channel"),
    }
    with db.connect() as conn:
        rows = store.list_tickets(conn, **filters, query=(q or "").strip() or None, limit=limit)
    return {"tickets": [store.to_json(row) for row in rows]}


@app.get("/api/tickets/{ticket_key}")
def get_ticket(ticket_key: str = Path(min_length=3, max_length=64)) -> dict[str, Any]:
    with db.connect() as conn:
        ticket = store.get_ticket(conn, ticket_key)
        if ticket is None:
            raise ApiError(404, "Ticket not found.")
        events = store.list_events(conn, ticket["id"])
    return {"ticket": store.to_json(ticket), "events": [store.to_json(event) for event in events]}


@app.get("/api/kb")
def list_kb() -> dict[str, Any]:
    with db.connect() as conn:
        return {"articles": store.list_kb_articles(conn)}


# Ticket intake ---------------------------------------------------------------------

@app.post("/api/demo/tickets")
def create_demo_ticket(payload: DemoTicketIn, request: Request) -> dict[str, Any]:
    started = time.perf_counter()
    if payload.website:
        raise ApiError(400, "Invalid request.")
    client_hash = _client_hash(request)
    inbound = channels.from_form(payload, channel=payload.channel, client_hash=client_hash, is_demo=True)
    with db.connect() as conn:
        _enforce_demo_limits(conn, client_hash)
        bundle = triage.process_ticket(conn, inbound)
    return {**_bundle_json(bundle), "timing_ms": round((time.perf_counter() - started) * 1000)}


@app.post("/api/webhooks/inbound")
def inbound_webhook(request: Request, raw: bytes = Depends(raw_body)) -> dict[str, Any]:
    _verify_signed_webhook(request, raw)
    payload = _parse(InboundTicketIn, raw)
    inbound = channels.from_form(
        payload,
        channel=payload.channel,
        client_hash=_client_hash(request),
        is_demo=False,
        source_id=f"inbound:{payload.external_id}" if payload.external_id else None,
    )
    with db.connect() as conn:
        bundle = triage.process_ticket(conn, inbound)
    return {"ticket": store.to_json(bundle.ticket), "duplicate": bundle.duplicate}


@app.post("/api/webhooks/email")
def email_webhook(request: Request, raw: bytes = Depends(raw_body)) -> dict[str, Any]:
    _verify_signed_webhook(request, raw)
    inbound = channels.from_postmark(_parse(PostmarkInboundIn, raw), client_hash=_client_hash(request))
    with db.connect() as conn:
        bundle = triage.process_ticket(conn, inbound)
    return {"ticket": store.to_json(bundle.ticket), "duplicate": bundle.duplicate}


@app.get("/api/webhooks/whatsapp")
def whatsapp_verify(
    mode: str = Query(default="", alias="hub.mode", max_length=32),
    token: str = Query(default="", alias="hub.verify_token", max_length=256),
    challenge: str = Query(default="", alias="hub.challenge", max_length=256),
) -> PlainTextResponse:
    if mode == "subscribe" and tokens_match(token, settings.whatsapp_verify_token):
        return PlainTextResponse(challenge)
    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/api/webhooks/whatsapp")
def whatsapp_webhook(request: Request, raw: bytes = Depends(raw_body)) -> dict[str, int]:
    try:
        verify_meta_signature(settings.whatsapp_app_secret, request.headers.get("x-hub-signature-256"), raw)
    except SignatureError as exc:
        logger.warning("whatsapp signature rejected reason=%s", exc)
        raise ApiError(401, "Invalid signature.") from exc
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"received": 0}

    messages = channels.from_whatsapp(payload, client_hash=_client_hash(request))
    created = 0
    if messages:
        with db.connect() as conn:
            for inbound in messages:
                # One bad message must not block the rest. Meta would retry the whole batch.
                try:
                    created += not triage.process_ticket(conn, inbound).duplicate
                except Exception:
                    logger.exception("whatsapp message failed source=%s", inbound.source_id)
    return {"received": created}


# Copilot, cron and MCP -------------------------------------------------------------

@app.post("/api/chat")
def chat(payload: ChatIn, request: Request) -> dict[str, Any]:
    if not chat_limiter.allow(_client_hash(request)):
        raise ApiError(
            429,
            "The copilot limit for this hour has been reached. Please try again later.",
            headers={"Retry-After": "3600"},
        )
    if not settings.anthropic_api_key:
        raise ApiError(503, "The copilot is not available right now.")
    try:
        return agent.run_agent(payload.messages).as_dict()
    except anthropic.APIError as exc:
        logger.warning("copilot failed error=%s", type(exc).__name__)
        raise ApiError(502, "The copilot could not answer just now. Please try again.") from exc


@app.get("/api/cron/sla")
def cron_sla(request: Request) -> dict[str, int]:
    _require_bearer(request, settings.cron_secret, "cron")
    with db.connect() as conn:
        return triage.check_sla(conn)


@app.post(MCP_PATH)
def mcp_endpoint(request: Request, raw: bytes = Depends(raw_body)) -> Response:
    _require_bearer(request, settings.mcp_token, "mcp")
    reply = mcp.handle(raw)
    if reply.body is None:
        return Response(status_code=202)
    return JSONResponse(reply.body, status_code=reply.status_code)


@app.get(MCP_PATH)
def mcp_get() -> JSONResponse:
    return JSONResponse(
        {"error": "This MCP server does not offer an event stream. Send JSON-RPC messages with POST."},
        status_code=405,
        headers={"Allow": "POST, OPTIONS"},
    )


@app.options(MCP_PATH)
def mcp_preflight() -> Response:
    return Response(status_code=204)
