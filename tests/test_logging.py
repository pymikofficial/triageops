"""Outbound webhook URLs are secrets, so the HTTP client must not log them."""

import logging

import httpx

from triageops import app  # noqa: F401  (importing the app applies the logging setup)


def test_httpx_request_lines_are_not_logged(caplog) -> None:
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    secret_url = "https://prod-01.uksouth.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=SECRET"
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(202)))
    with caplog.at_level(logging.INFO):
        client.post(secret_url, json={})
    assert "SECRET" not in caplog.text
