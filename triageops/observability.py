"""Structured JSON logs and a request id on every API request.

Each log line is one JSON object on stderr, which Vercel, Docker and journald all
collect as is. Every line written while a request is being handled carries that
request's id, and the id goes back to the caller as ``X-Request-ID`` so a support
conversation can point straight at the matching log lines.

A caller may send its own ``X-Request-ID`` (an upstream proxy or n8n, for example).
It is reused only when it looks like an id, so log lines cannot be forged with
newlines or JSON through that header. Otherwise a new id is generated.

Access lines record the method, the path without its query string, the status and
the duration. Query strings and client IPs are left out, because the ticket search
box puts free text in the query string and raw IPs are never stored.
"""

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_INCOMING_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

access_logger = logging.getLogger("triageops.access")

# Attributes every LogRecord has. Anything else was passed with extra= and is logged as a field.
# uvicorn adds color_message, a copy of the message with terminal colour codes.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "color_message"}


def new_request_id() -> str:
    return uuid.uuid4().hex


def accept_request_id(value: str | None) -> str:
    """Reuse a well formed incoming id, otherwise make a new one."""
    if value and _INCOMING_ID.fullmatch(value):
        return value
    return new_request_id()


def current_request_id() -> str | None:
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, request id and extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            entry["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in entry:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class _JsonHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is at the time, and marks that configure_logging ran."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self) -> Any:  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        pass


def configure_logging(level: int = logging.INFO) -> None:
    """Send every log record to stderr as JSON. Safe to call more than once.

    Under uvicorn (the Docker and systemd kits), uvicorn's own loggers are routed
    through the same JSON handler, and its access log is switched off: it records
    client IPs and full query strings, and ``RequestContextMiddleware`` already
    writes one access line per request without them. uvicorn configures logging
    before it imports the app, so this runs after it and wins.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    if any(isinstance(handler, _JsonHandler) for handler in root.handlers):
        return
    handler = _JsonHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


class RequestContextMiddleware:
    """Pure ASGI middleware: sets the request id, adds the response header, logs one access line.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so the context variable is set before
    any other middleware or route runs, including the thread pool used by sync routes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        request_id = accept_request_id(incoming)
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
            access_logger.log(
                level,
                "%s %s %d",
                scope.get("method", ""),
                scope.get("path", ""),
                status,
                extra={
                    "event": "http_request",
                    "method": scope.get("method", ""),
                    "path": scope.get("path", ""),
                    "status": status,
                    "duration_ms": duration_ms,
                },
            )
            request_id_var.reset(token)
