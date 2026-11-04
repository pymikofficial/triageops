"""Measure knowledge base retrieval against the real database: vector only versus hybrid.

Runs the same 20 queries as tests/test_retrieval_quality.py, but with real
gte-small embeddings from EMBED_URL and the real match_kb and match_kb_hybrid
functions. Read only. It needs migration 004 applied and the same environment
variables as the API (DATABASE_URLS, EMBED_URL, EMBED_SECRET).

Example
    set -a; source .env; set +a
    python scripts/eval_retrieval.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from retrieval_cases import RETRIEVAL_CASES  # noqa: E402

from triageops import db, rag, store  # noqa: E402

K = 3


def main() -> int:
    vector_hits = hybrid_hits = 0
    with db.connect() as conn:
        for query, expected in RETRIEVAL_CASES:
            (vector,) = rag.embed([query])
            literal = rag.vector_literal(vector)
            vector_slugs = [row["slug"] for row in store.match_kb(conn, literal, K)]
            hybrid_slugs = [row["slug"] for row in store.match_kb_hybrid(conn, literal, query, K)]
            vector_hits += expected in vector_slugs
            hybrid_hits += expected in hybrid_slugs
            marks = f"{'hit ' if expected in vector_slugs else 'miss'} {'hit ' if expected in hybrid_slugs else 'miss'}"
            print(f"{marks}  {expected:28} {query[:60]}")
    total = len(RETRIEVAL_CASES)
    print(f"\nhit rate at {K}: vector only {vector_hits / total:.2f}, hybrid {hybrid_hits / total:.2f} ({total} queries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
