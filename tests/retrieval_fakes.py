"""Deterministic stand-ins for the two retrievers, so retrieval quality can be measured offline.

``FakeEmbedder`` plays gte-small. It hashes text features into 384 signed
dimensions and normalises the vector. No hashing trick behaves like a real
semantic model, so it comes in five feature modes (whole words, words without
stop words, stems, character trigrams, stems plus trigrams) and the quality test
checks every mode, rather than one picked because it flatters hybrid search.
It is deterministic across runs and machines because it uses BLAKE2b, not ``hash()``.

``KeywordIndex`` plays the ``search_tsv`` column from migration 004. It mirrors
what Postgres does closely enough to compare rankings: English stop words are
dropped, words are reduced to a rough stem, title words weigh 1.0, category
words 0.4 and body words 0.2 (the default ``ts_rank`` weights for A, B and C),
query terms are ORed together, and there is no IDF, just as ``ts_rank_cd`` has none.
"""

import hashlib
import math
import re
from collections.abc import Sequence

from triageops.hybrid import CANDIDATE_COUNT, reciprocal_rank_fusion
from triageops.kb_seed import Article
from triageops.rag import EMBEDDING_DIMENSIONS

STOP_WORDS = frozenset(
    """a about after again all am an and any are as at be because been before being both but by can
    could did do does doing down during each few for from further had has have having he her here hers
    him his how i if in into is it its just me more most my myself no nor not now of off on once only
    or other our ours out over own same she should so some such than that the their them then there
    these they this those through to too under until up very was we were what when where which while
    who whom why will with would you your yours""".split()
)
_SUFFIXES = ("ingly", "edly", "ing", "ies", "ied", "es", "ed", "ly", "s")
WEIGHTS = {"title": 1.0, "category": 0.4, "content": 0.2}


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def lexemes(text: str) -> list[str]:
    return [stem(word) for word in words(text) if word not in STOP_WORDS]


def trigrams(text: str) -> list[str]:
    features = []
    for word in words(text):
        padded = f"<{word}>"
        features.extend(padded[i : i + 3] for i in range(len(padded) - 2))
    return features


EMBEDDER_MODES = {
    "words": words,
    "words_without_stop_words": lambda text: [word for word in words(text) if word not in STOP_WORDS],
    "stems": lexemes,
    "trigrams": trigrams,
    "stems_and_trigrams": lambda text: lexemes(text) + trigrams(text),
}


class FakeEmbedder:
    def __init__(self, mode: str = "words", dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.mode = mode
        self.dimensions = dimensions
        self._features = EMBEDDER_MODES[mode]

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature in self._features(text):
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class KeywordIndex:
    def __init__(self, articles: Sequence[Article]) -> None:
        self.documents: dict[str, dict[str, float]] = {}
        for article in articles:
            weights: dict[str, float] = {}
            counts: dict[str, int] = {}
            for field, text in (("title", article.title), ("category", article.category), ("content", article.content)):
                for lexeme in lexemes(text):
                    weights[lexeme] = max(weights.get(lexeme, 0.0), WEIGHTS[field])
                    counts[lexeme] = counts.get(lexeme, 0) + 1
            self.documents[article.slug] = {lexeme: weights[lexeme] * (1 + math.log(counts[lexeme])) for lexeme in weights}

    def rank(self, query: str, limit: int = CANDIDATE_COUNT) -> list[str]:
        terms = set(lexemes(query))
        scored = []
        for slug, document in self.documents.items():
            score = sum(document[term] for term in terms if term in document)
            if score > 0:
                scored.append((score, slug))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [slug for _, slug in scored[:limit]]


class OfflineKnowledgeBase:
    """The whole knowledge base in memory, searchable vector only or hybrid."""

    def __init__(self, articles: Sequence[Article], embedder: FakeEmbedder | None = None) -> None:
        self.embedder = embedder or FakeEmbedder()
        self.vectors = {article.slug: self.embedder.embed(article.embedding_text()) for article in articles}
        self.keywords = KeywordIndex(articles)

    def vector_rank(self, query: str, limit: int = CANDIDATE_COUNT) -> list[str]:
        query_vector = self.embedder.embed(query)
        scored = sorted(self.vectors.items(), key=lambda item: (-cosine(query_vector, item[1]), item[0]))
        return [slug for slug, _ in scored[:limit]]

    def vector_only(self, query: str, count: int = 3) -> list[str]:
        return self.vector_rank(query, count)

    def hybrid(self, query: str, count: int = 3) -> list[str]:
        fused = reciprocal_rank_fusion([self.vector_rank(query), self.keywords.rank(query)])
        return [slug for slug, _ in fused[:count]]
