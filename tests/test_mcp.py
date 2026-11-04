"""The MCP JSON-RPC endpoint, called through the FastAPI app with the database faked."""

import json
from contextlib import contextmanager
from typing import Any

import pytest
from fakes import MCP_TOKEN
from fastapi.testclient import TestClient

from triageops import db, store
from triageops.app import app
from triageops.config import settings

STATS = {"total": 3, "open": 2, "by_priority": {"P1": 1, "P2": 0, "P3": 2, "P4": 0}}
AUTH = {"Authorization": f"Bearer {MCP_TOKEN}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    @contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(store, "queue_stats", lambda conn: STATS)
    monkeypatch.setattr(store, "get_ticket", lambda conn, key: None)
    return TestClient(app)


def rpc(client: TestClient, method: str, params: Any = None, *, message_id: Any = 1, headers: dict | None = None):
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if message_id is not None:
        message["id"] = message_id
    if params is not None:
        message["params"] = params
    return client.post("/api/mcp", json=message, headers=AUTH if headers is None else headers)


@pytest.mark.parametrize(("requested", "agreed"), [("2025-03-26", "2025-03-26"), ("1999-01-01", "2025-06-18")])
def test_initialize_negotiates_the_protocol_version(client: TestClient, requested: str, agreed: str) -> None:
    response = rpc(client, "initialize", {"protocolVersion": requested, "capabilities": {}, "clientInfo": {"name": "t"}})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 1 and body["jsonrpc"] == "2.0"
    result = body["result"]
    assert result["protocolVersion"] == agreed
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"] == {"name": "triageops", "version": "1.0.0"}


def test_notifications_get_202_with_no_body(client: TestClient) -> None:
    response = rpc(client, "notifications/initialized", message_id=None)
    assert response.status_code == 202
    assert response.content == b""


def test_ping(client: TestClient) -> None:
    assert rpc(client, "ping").json()["result"] == {}


def test_tools_list_describes_four_read_only_tools(client: TestClient) -> None:
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["get_queue_stats", "search_tickets", "get_ticket", "search_knowledge_base"]
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["description"]


def test_tools_call_returns_json_text_content(client: TestClient) -> None:
    result = rpc(client, "tools/call", {"name": "get_queue_stats", "arguments": {}}).json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert json.loads(result["content"][0]["text"]) == STATS


def test_a_missing_ticket_is_a_tool_error_not_a_protocol_error(client: TestClient) -> None:
    result = rpc(client, "tools/call", {"name": "get_ticket", "arguments": {"ref": "TKT-0000-0000"}}).json()["result"]
    assert result["isError"] is True
    assert "No ticket matches" in result["content"][0]["text"]


@pytest.mark.parametrize(
    "params",
    [
        {"name": "delete_everything", "arguments": {}},
        {"name": "search_tickets", "arguments": {"limit": 500}},
        {"name": "search_tickets", "arguments": {"priority": "P9"}},
        {"name": "get_ticket", "arguments": {}},
        {"name": "get_queue_stats", "arguments": {"unexpected": True}},
        {"arguments": {}},
    ],
)
def test_bad_tool_calls_return_invalid_params(client: TestClient, params: dict) -> None:
    body = rpc(client, "tools/call", params).json()
    assert body["error"]["code"] == -32602
    assert body["id"] == 1


def test_params_must_be_an_object(client: TestClient) -> None:
    assert rpc(client, "tools/list", ["not", "an", "object"]).json()["error"]["code"] == -32602


def test_unknown_method_returns_method_not_found(client: TestClient) -> None:
    body = rpc(client, "resources/list").json()
    assert body["error"]["code"] == -32601


def test_invalid_json_is_a_parse_error(client: TestClient) -> None:
    response = client.post("/api/mcp", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_a_message_without_jsonrpc_version_is_invalid(client: TestClient) -> None:
    response = client.post("/api/mcp", json={"id": 1, "method": "ping"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}, {"Authorization": MCP_TOKEN}])
def test_requests_without_the_bearer_token_are_rejected(client: TestClient, headers: dict) -> None:
    response = rpc(client, "tools/list", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert "result" not in response.json()


def test_an_unset_server_token_rejects_everyone(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_token", "")
    assert rpc(client, "ping", headers={"Authorization": "Bearer "}).status_code == 401


def test_get_is_not_allowed(client: TestClient) -> None:
    response = client.get("/api/mcp")
    assert response.status_code == 405
    assert "POST" in response.headers["allow"]


def test_cors_allows_any_origin_for_mcp_only(client: TestClient) -> None:
    preflight = client.options("/api/mcp", headers={"Origin": "https://inspector.example", "Access-Control-Request-Method": "POST"})
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert "Authorization" in preflight.headers["access-control-allow-headers"]
    assert "access-control-allow-origin" not in client.get("/api/kb-missing").headers
