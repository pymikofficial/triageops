"""Postgres connection helper for the Supabase transaction pooler.

The pooler runs in transaction mode, so the connection settings avoid anything
that relies on session state: no prepared statements (``prepare_threshold=None``)
and autocommit, so each statement is its own short transaction. The pooler may
ignore the ``options`` startup parameter, so SQL in ``store`` also qualifies the
vector type with its schema instead of relying on ``search_path``.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from .config import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5

# The first URL that connected. Warm serverless instances try it first.
_preferred_url: str | None = None


class DatabaseUnavailable(RuntimeError):
    """Raised when no configured database URL accepts a connection."""


Connection = psycopg.Connection[dict]


def _candidate_urls() -> list[str]:
    urls = list(settings.database_urls)
    if _preferred_url in urls:
        urls.remove(_preferred_url)
        urls.insert(0, _preferred_url)
    return urls


def _host_of(url: str) -> str:
    """Return only the host of a URL, so passwords never reach the logs."""
    try:
        return str(conninfo_to_dict(url).get("host", "unknown"))
    except psycopg.ProgrammingError:
        return "unparseable url"


def _open(url: str) -> Connection:
    return psycopg.connect(
        url,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        prepare_threshold=None,
        autocommit=True,
        row_factory=dict_row,
        options="-c search_path=public,extensions",
    )


@contextmanager
def connect() -> Iterator[Connection]:
    """Yield a connection from the first reachable URL in ``DATABASE_URLS``."""
    global _preferred_url
    urls = _candidate_urls()
    if not urls:
        raise DatabaseUnavailable("DATABASE_URLS is not set")

    conn: Connection | None = None
    for url in urls:
        try:
            conn = _open(url)
        except psycopg.OperationalError as exc:
            logger.warning("database connect failed host=%s error=%s", _host_of(url), type(exc).__name__)
            continue
        _preferred_url = url
        break
    if conn is None:
        raise DatabaseUnavailable("no database URL accepted a connection")

    try:
        yield conn
    finally:
        conn.close()
