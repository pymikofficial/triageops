"""Retrieval over the knowledge base: embeddings from a Supabase Edge Function, search in pgvector.

The Edge Function runs gte-small and returns normalised 384 dimension vectors,
so cosine similarity in ``match_kb`` is a direct relevance score.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from . import store
from .config import settings
from .db import Connection

EMBED_TIMEOUT_SECONDS = 8.0
EMBEDDING_DIMENSIONS = 384
DEFAULT_MATCH_COUNT = 3
# gte-small reads at most 512 tokens, so longer text adds cost without signal.
QUERY_CHAR_LIMIT = 2000


class EmbeddingError(RuntimeError):
    """The embeddings service is not configured, unreachable or returned bad data."""


@dataclass(frozen=True)
class KbMatch:
    slug: str
    title: str
    category: str
    content: str
    similarity: float

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


def search(conn: Connection, text: str, count: int = DEFAULT_MATCH_COUNT) -> list[KbMatch]:
    """Top ``count`` articles by cosine similarity. Raises ``EmbeddingError``."""
    (vector,) = embed([text[:QUERY_CHAR_LIMIT]])
    rows = store.match_kb(conn, vector_literal(vector), count)
    return [
        KbMatch(
            slug=row["slug"],
            title=row["title"],
            category=row["category"],
            content=row["content"],
            similarity=float(row["similarity"]),
        )
        for row in rows
    ]
