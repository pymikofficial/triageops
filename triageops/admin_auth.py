"""Authentication for the analyst console.

One shared ``ADMIN_TOKEN`` is sent as ``Authorization: Bearer <token>`` and
compared in constant time. Failed attempts are counted per hashed client IP,
and a client that fails too often is locked out for the rest of the window,
even if it then sends the right token, so the token cannot be guessed by brute
force. Like the copilot limit, the count is held in memory per instance.
"""

import logging
import math
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from .config import settings
from .security import bearer_token_matches, client_ip, hash_client

logger = logging.getLogger("triageops.admin")

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 15 * 60


class FailedAttemptLimiter:
    """Counts failures per key in a sliding window. Successes are never counted."""

    _MAX_KEYS = 10_000

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        failures = self._failures.get(key, deque())
        cutoff = now - self.window
        while failures and failures[0] <= cutoff:
            failures.popleft()
        return failures

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Seconds until the key may try again, or 0 when it is not locked out."""
        current = time.monotonic() if now is None else now
        with self._lock:
            failures = self._recent(key, current)
            if len(failures) < self.limit:
                return 0
            return max(math.ceil(failures[-self.limit] + self.window - current), 1)

    def record_failure(self, key: str, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            if len(self._failures) > self._MAX_KEYS:
                cutoff = current - self.window
                for stale in [k for k, f in self._failures.items() if not f or f[-1] <= cutoff]:
                    del self._failures[stale]
            failures = self._recent(key, current)
            failures.append(current)
            self._failures[key] = failures

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


failed_logins = FailedAttemptLimiter(MAX_FAILED_ATTEMPTS, LOCKOUT_WINDOW_SECONDS)


def _client_key(request: Request) -> str:
    peer = request.client.host if request.client else None
    return hash_client(client_ip(request.headers, peer), settings.ip_hash_salt)


def require_admin(request: Request) -> None:
    """FastAPI dependency for every ``/api/admin`` route."""
    key = _client_key(request)
    wait = failed_logins.retry_after(key)
    if wait:
        raise HTTPException(
            429,
            "Too many failed sign in attempts. Please wait before trying again.",
            headers={"Retry-After": str(wait)},
        )
    if not settings.admin_token:
        logger.warning("ADMIN_TOKEN is not configured, the analyst console is disabled")
        raise HTTPException(503, "The analyst console is not configured.")
    if not bearer_token_matches(request.headers.get("authorization"), settings.admin_token):
        failed_logins.record_failure(key)
        logger.warning("analyst console sign in rejected")
        raise HTTPException(401, "Unauthorised.", headers={"WWW-Authenticate": 'Bearer realm="admin"'})
