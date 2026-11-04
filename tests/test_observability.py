"""Structured JSON logs and the X-Request-ID header on every API request."""

import json
import logging
import re
import sys

import pytest
from fakes import FakeStore
from fastapi.testclient import TestClient

from triageops import app as app_module
from triageops import observability, store
from triageops.app import MAX_BODY_BYTES, app
from triageops.observability import JsonFormatter, accept_request_id, request_id_var

HEX_ID = re.compile(r"[0-9a-f]{32}")


@pytest.fixture
def client(fake_store: FakeStore) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def access_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "triageops.access"]


# Request ids ----------------------------------------------------------------------

def test_every_response_gets_a_new_request_id(client: TestClient) -> None:
    first = client.get("/api/health").headers["x-request-id"]
    second = client.get("/api/health").headers["x-request-id"]
    assert HEX_ID.fullmatch(first) and HEX_ID.fullmatch(second)
    assert first != second


def test_a_well_formed_incoming_id_is_reused(client: TestClient) -> None:
    response = client.get("/api/health", headers={"X-Request-ID": "n8n-run-4821.step_2"})
    assert response.headers["x-request-id"] == "n8n-run-4821.step_2"


@pytest.mark.parametrize("incoming", ["short", "x" * 65, 'abc"}{"level":"error', "has space in it", ""])
def test_unsafe_incoming_ids_are_replaced(incoming: str) -> None:
    assert HEX_ID.fullmatch(accept_request_id(incoming))


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("get", "/api/tickets/TKT-0000-0000", 404), ("get", "/api/nowhere", 404), ("get", "/api/tickets?limit=0", 422)],
)
def test_error_responses_carry_the_id_too(client: TestClient, method: str, path: str, status: int) -> None:
    response = getattr(client, method)(path)
    assert response.status_code == status
    assert HEX_ID.fullmatch(response.headers["x-request-id"])


def test_oversized_bodies_and_crashes_carry_the_id(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    too_big = client.post("/api/demo/tickets", content=b"{}", headers={"content-length": str(MAX_BODY_BYTES + 1)})
    assert too_big.status_code == 413
    assert "x-request-id" in too_big.headers

    def explode(conn):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "queue_stats", explode)
    crashed = client.get("/api/stats")
    assert crashed.status_code == 500
    assert "x-request-id" in crashed.headers


def test_security_headers_are_still_set(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.headers["x-frame-options"] == "DENY"
    assert "x-request-id" in response.headers


# Access log -----------------------------------------------------------------------

def test_one_access_line_per_request_without_the_query_string(client: TestClient, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="triageops.access"):
        response = client.get("/api/health?q=jane.doe@example.com")
    (line,) = access_lines(caplog)
    assert line.path == "/api/health"
    assert line.method == "GET"
    assert line.status == 200
    assert line.duration_ms >= 0
    assert line.event == "http_request"
    assert "example.com" not in line.getMessage()
    assert request_id_var.get() is None, "the id must not leak out of the request"
    assert HEX_ID.fullmatch(response.headers["x-request-id"])


def test_access_level_follows_the_status(client: TestClient, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="triageops.access"):
        client.get("/api/health")
        client.get("/api/nowhere")
    assert [line.levelname for line in access_lines(caplog)] == ["INFO", "WARNING"]


def test_logs_inside_a_sync_route_carry_the_request_id(client: TestClient, capfd, monkeypatch) -> None:
    seen: list[str | None] = []

    def stats(conn):
        seen.append(observability.current_request_id())
        logging.getLogger("triageops.store").info("stats read")
        return {"total": 0}

    monkeypatch.setattr(store, "queue_stats", stats)
    capfd.readouterr()
    response = client.get("/api/stats", headers={"X-Request-ID": "trace-0001-abcd"})
    assert seen == ["trace-0001-abcd"]
    entries = [json.loads(line) for line in capfd.readouterr().err.splitlines() if line.startswith("{")]
    by_logger = {entry["logger"]: entry for entry in entries}
    assert by_logger["triageops.store"]["request_id"] == "trace-0001-abcd"
    assert by_logger["triageops.access"]["request_id"] == "trace-0001-abcd"
    assert by_logger["triageops.access"]["status"] == 200
    assert response.headers["x-request-id"] == "trace-0001-abcd"


# JSON formatter -------------------------------------------------------------------

def make_record(message: str = "hello %s", args: tuple = ("world",), **extra) -> logging.LogRecord:
    record = logging.LogRecord("triageops.test", logging.WARNING, __file__, 1, message, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_writes_one_json_object_per_line() -> None:
    line = JsonFormatter().format(make_record(status=503, path="/api/health"))
    assert "\n" not in line
    entry = json.loads(line)
    assert entry["message"] == "hello world"
    assert entry["level"] == "warning"
    assert entry["logger"] == "triageops.test"
    assert entry["status"] == 503
    assert entry["path"] == "/api/health"
    assert entry["ts"].endswith("+00:00")
    assert "request_id" not in entry


def test_formatter_adds_the_current_request_id() -> None:
    token = request_id_var.set("abc12345")
    try:
        assert json.loads(JsonFormatter().format(make_record()))["request_id"] == "abc12345"
    finally:
        request_id_var.reset(token)


def test_formatter_escapes_newlines_in_messages_and_includes_tracebacks() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "line one\nline two", None, sys.exc_info())
    line = JsonFormatter().format(record)
    assert "\n" not in line
    entry = json.loads(line)
    assert entry["message"] == "line one\nline two"
    assert "ValueError: bad" in entry["exception"]


def test_formatter_serialises_odd_values_as_text() -> None:
    entry = json.loads(JsonFormatter().format(make_record(when=object())))
    assert entry["when"].startswith("<object object")


def test_configure_logging_is_idempotent() -> None:
    observability.configure_logging()
    observability.configure_logging()
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, observability._JsonHandler)]
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, JsonFormatter)


def test_uvicorn_logs_go_through_json_and_its_access_log_is_off() -> None:
    noisy = logging.getLogger("uvicorn.error")
    noisy.addHandler(logging.StreamHandler())
    noisy.propagate = False
    observability.configure_logging()
    assert noisy.handlers == [] and noisy.propagate
    assert logging.getLogger("uvicorn.access").disabled


def test_the_app_installed_the_json_handler() -> None:
    assert app_module.logger.name == "triageops"
    assert any(isinstance(h, observability._JsonHandler) for h in logging.getLogger().handlers)
