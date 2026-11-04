"""Retrieval over the knowledge base: embeddings from a Supabase Edge Function, search in Postgres.

The Edge Function runs gte-small and returns normalised 384 dimension vectors,
so cosine similarity in ``match_kb`` is a direct relevance score. When migration
004 is applied, ``match_kb_hybrid`` also runs full text search and fuses both
rankings with reciprocal rank fusion. Until then search falls back to vector only.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg
from psycopg import errors

from . import hybrid, store
from .config import settings
from .db import Connection

logger = logging.getLogger(__name__)

EMBED_TIMEOUT_SECONDS = 8.0
EMBEDDING_DIMENSIONS = 384
DEFAULT_MATCH_COUNT = 3
# gte-small reads at most 512 tokens, so longer text adds cost without signal.
QUERY_CHAR_LIMIT = 2000


# Errors that mean migration 004 is missing or not granted, rather than a passing fault.
_HYBRID_MISSING = (errors.UndefinedFunction, errors.UndefinedColumn, errors.InsufficientPrivilege)


class EmbeddingError(RuntimeError):
    """The embeddings service is not configured, unreachable or returned bad data."""


@dataclass(frozen=True)
class KbMatch:
    slug: str
    title: str
    category: str
    content: str
    similarity: float
    retrieval: str = "vector"

    def ref(self) -> dict[str, Any]:
        """The compact form stored in ``tickets.kb_refs``."""
        return {"slug": self.slug, "title": self.title, "similarity": round(self.similarity, 3)}


def is_configured() -> bool:
    return bool(settings.embed_url and settings.embed_secret)


def embed(texts: Sequence[str], *, timeout: float = EMBED_TIMEOUT_SECONDS) -> list[list[float]]:
    """Embed a batch of texts in one request."""
    if not is_configured():
        raise EmbeddingError("EMBED_URL or EMBED_SECRET is not set")
    try:
        response = httpx.post(
            settings.embed_url,
            headers={"x-embed-secret": settings.embed_secret},
            json={"input": list(texts)},
            timeout=timeout,
        )
        response.raise_for_status()
        vectors = response.json()["embeddings"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EmbeddingError(f"embedding request failed ({type(exc).__name__})") from exc

    valid = isinstance(vectors, list) and len(vectors) == len(texts) and all(
        isinstance(vector, list) and len(vector) == EMBEDDING_DIMENSIONS for vector in vectors
    )
    if not valid:
        raise EmbeddingError("embedding response had an unexpected shape")
    return vectors


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector text form, for example ``[0.12,-0.03,...]``."""
    return "[" + ",".join(f"{float(value):.7g}" for value in vector) + "]"


def _match(row: dict[str, Any], retrieval: str) -> KbMatch:
    return KbMatch(
        slug=row["slug"],
        title=row["title"],
        category=row["category"],
        content=row["content"],
        similarity=float(row["similarity"] or 0.0),
        retrieval=retrieval,
    )


def search(conn: Connection, text: str, count: int = DEFAULT_MATCH_COUNT) -> list[KbMatch]:
    """Top ``count`` articles, hybrid when available and vector only otherwise.

    Raises ``EmbeddingError`` when the query cannot be embedded.
    """
    query = text[:QUERY_CHAR_LIMIT]
    (vector,) = embed([query])
    literal = vector_literal(vector)
    if hybrid.switch.enabled():
        try:
            rows = store.match_kb_hybrid(conn, literal, query, count)
        except _HYBRID_MISSING as exc:
            hybrid.switch.disable(type(exc).__name__)
        except psycopg.Error as exc:
            logger.warning("hybrid knowledge base search failed, using vector only error=%s", type(exc).__name__)
        else:
            return [_match(row, "hybrid") for row in rows]
    return [_match(row, "vector") for row in store.match_kb(conn, literal, count)]
