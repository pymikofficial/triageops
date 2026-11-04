"""Hybrid retrieval support: reciprocal rank fusion and the vector only fallback switch.

Postgres does the real fusion inside ``match_kb_hybrid`` (migration 004). This
module keeps a Python mirror of the same arithmetic, so the offline retrieval
quality test and anyone reading the code can see exactly how ranks combine.

Until the owner applies migration 004 the function does not exist. ``rag.search``
then falls back to the vector only ``match_kb`` and this module remembers that for
a few minutes, so a warm instance does not pay for a failing call on every ticket,
but still notices soon after the migration lands.
"""

import logging
import time
from collections.abc import Callable, Hashable, Iterable, Sequence
from typing import TypeVar

logger = logging.getLogger(__name__)

# 60 is the constant from the original RRF paper (Cormack, Clarke and Buettcher, 2009)
# and the value most search engines ship. It damps the gap between rank 1 and rank 2.
RRF_K = 60
# How many articles each retriever ranks before fusion. Mirrors candidate_count in SQL.
CANDIDATE_COUNT = 20
# After a failed hybrid call, use vector only search for this long before trying again.
RETRY_AFTER_SECONDS = 300.0

Key = TypeVar("Key", bound=Hashable)


def reciprocal_rank_fusion(rankings: Iterable[Sequence[Key]], k: int = RRF_K) -> list[tuple[Key, float]]:
    """Fuse several best first rankings into one, highest score first.

    Each item scores ``1 / (k + rank)`` (rank starts at 1) for every list it appears
    in, and the scores are summed. Ties keep the order in which items were first seen,
    which is stable for a given input.
    """
    scores: dict[Key, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


class HybridSwitch:
    """Remembers whether ``match_kb_hybrid`` is usable, with a timed retry."""

    def __init__(self, retry_after: float = RETRY_AFTER_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self.retry_after = retry_after
        self.clock = clock
        self._disabled_until = 0.0

    def enabled(self) -> bool:
        return self.clock() >= self._disabled_until

    def disable(self, reason: str) -> None:
        if self.enabled():
            logger.warning(
                "hybrid knowledge base search unavailable, using vector only search for %ds reason=%s",
                int(self.retry_after),
                reason,
            )
        self._disabled_until = self.clock() + self.retry_after

    def reset(self) -> None:
        self._disabled_until = 0.0


switch = HybridSwitch()
