"""Run the real triage pipeline over the labelled dataset with no database and no embeddings service.

``triage.process_ticket`` runs unchanged: masking, the forced tool use Claude
call, output repair, the business rules and the SLA. Three things are swapped
for local stand-ins, so the only network call is the one to Claude.

1. ``triageops.store`` writes go to ``EvalStore``, a thread safe in-memory store.
2. ``rag.search`` uses ``LexicalRetriever`` over the twelve seeded runbooks, in
   place of gte-small embeddings in Supabase.
3. ``ALERT_WEBHOOK_URL`` is cleared, so no alert leaves the machine.
"""

import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from triageops import rag, store, triage
from triageops.config import settings
from triageops.kb_seed import KB_ARTICLES
from triageops.rag import KbMatch
from triageops.schemas import InboundTicket
from triageops.store import TICKET_COLUMNS
from triageops.taxonomy import CATEGORIES, CHANNELS, PRIORITIES, TEAMS

EVALS_DIR = Path(__file__).resolve().parent
DATASET_PATH = EVALS_DIR / "dataset.jsonl"
DEFAULT_CONCURRENCY = 4
# Rules that only record what happened, not a change to the triage.
_BOOKKEEPING_RULES = {"sla", "escalate", "injection_human_review"}


# Dataset --------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    id: str
    channel: str
    customer: str | None
    subject: str
    body: str
    category: str
    priority: str
    team: str
    injection: bool
    tags: tuple[str, ...] = ()
    rationale: str = ""

    @property
    def expected(self) -> dict[str, Any]:
        return {"category": self.category, "priority": self.priority, "team": self.team, "injection": self.injection}


class DatasetError(ValueError):
    """A row in the dataset is missing a field or uses a label outside the taxonomy."""


def parse_case(row: dict[str, Any], line: int = 0) -> Case:
    where = f"line {line} ({row.get('id', 'no id')})"
    for key in ("id", "channel", "subject", "body", "expected_category", "expected_priority", "expected_team"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise DatasetError(f"{where}: {key} is missing")
    if not isinstance(row.get("injection"), bool):
        raise DatasetError(f"{where}: injection must be true or false")
    for key, allowed in (
        ("channel", CHANNELS),
        ("expected_category", CATEGORIES),
        ("expected_priority", PRIORITIES),
        ("expected_team", TEAMS),
    ):
        if row[key] not in allowed:
            raise DatasetError(f"{where}: {key} {row[key]!r} is not in the taxonomy")
    return Case(
        id=row["id"],
        channel=row["channel"],
        customer=row.get("customer") or None,
        subject=row["subject"],
        body=row["body"],
        category=row["expected_category"],
        priority=row["expected_priority"],
        team=row["expected_team"],
        injection=row["injection"],
        tags=tuple(row.get("tags") or ()),
        rationale=row.get("rationale") or "",
    )


def load_dataset(path: Path = DATASET_PATH) -> list[Case]:
    cases: list[Case] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            cases.append(parse_case(json.loads(line), number))
    ids = Counter(case.id for case in cases)
    duplicates = sorted(key for key, count in ids.items() if count > 1)
    if duplicates:
        raise DatasetError(f"duplicate ids: {', '.join(duplicates)}")
    return cases


def dataset_digest(path: Path = DATASET_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Fake database ---------------------------------------------------------------------

class EvalStore:
    """The subset of ``triageops.store`` the pipeline calls, held in memory behind a lock."""

    def __init__(self, created_at: datetime | None = None) -> None:
        self.created_at = created_at or datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
        self.tickets: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def insert_ticket(self, conn: Any, **fields: Any) -> dict[str, Any]:
        with self._lock:
            ticket = dict.fromkeys(TICKET_COLUMNS)
            ticket.update(
                id=uuid.uuid4(),
                ref=f"EVAL-{len(self.tickets) + 1:04d}",
                created_at=self.created_at,
                updated_at=self.created_at,
                status="new",
                next_steps=[],
                kb_refs=[],
                sla_breached=False,
                injection_suspected=False,
                **fields,
            )
            ticket.pop("client_hash", None)
            self.tickets[str(ticket["id"])] = ticket
            self.events[str(ticket["id"])] = []
            return dict(ticket)

    def update_ticket(self, conn: Any, ticket_id: Any, fields: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.tickets[str(ticket_id)].update(fields)
            return dict(self.tickets[str(ticket_id)])

    def mark_failed(self, conn: Any, ticket_id: Any) -> None:
        with self._lock:
            self.tickets[str(ticket_id)]["status"] = "failed"

    def find_ticket_by_source(self, conn: Any, source_id: str) -> dict[str, Any] | None:
        return None  # Every eval case is a fresh ticket.

    def add_events(self, conn: Any, ticket_id: Any, events: list[tuple[str, dict[str, Any]]]) -> None:
        with self._lock:
            log = self.events.setdefault(str(ticket_id), [])
            for kind, detail in events:
                log.append({"id": len(log) + 1, "created_at": self.created_at, "type": kind, "detail": dict(detail)})

    def list_events(self, conn: Any, ticket_id: Any) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self.events.get(str(ticket_id), [])]


_STORE_FUNCTIONS = ("insert_ticket", "update_ticket", "mark_failed", "find_ticket_by_source", "add_events", "list_events")


# Offline retrieval -----------------------------------------------------------------

_STOPWORDS = frozenset(
    "the and for are but not you all any can her was one our out has have had his how its may new now see way who "
    "did get got let put say she too use this that with from they them then than there their what when which will "
    "would could should into only also been were your about after again before being every other some such very "
    "more most just over under while please thanks hello morning".split()
)
_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> Counter[str]:
    return Counter(word for word in _WORD.findall(text.lower()) if len(word) > 2 and word not in _STOPWORDS)


class LexicalRetriever:
    """TF-IDF cosine similarity over the knowledge base, a deterministic stand-in for pgvector."""

    def __init__(self, articles: Sequence[Any] = KB_ARTICLES) -> None:
        self.articles = list(articles)
        documents = [_terms(f"{a.title} {a.title} {a.content}") for a in self.articles]
        frequency = Counter(term for doc in documents for term in doc)
        total = len(documents)
        self.idf = {term: math.log((1 + total) / (1 + count)) + 1 for term, count in frequency.items()}
        self.vectors = [self._weigh(doc) for doc in documents]

    def _weigh(self, terms: Counter[str]) -> dict[str, float]:
        weights = {term: count * self.idf.get(term, 0.0) for term, count in terms.items()}
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        return {term: w / norm for term, w in weights.items() if w}

    def search(self, conn: Any, text: str, count: int = rag.DEFAULT_MATCH_COUNT) -> list[KbMatch]:
        query = self._weigh(_terms(text))
        scored = [
            (sum(weight * vector.get(term, 0.0) for term, weight in query.items()), index)
            for index, vector in enumerate(self.vectors)
        ]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [
            KbMatch(
                slug=self.articles[i].slug,
                title=self.articles[i].title,
                category=self.articles[i].category,
                content=self.articles[i].content,
                similarity=round(similarity, 3),
            )
            for similarity, i in scored[:count]
            if similarity > 0
        ]


# Running ---------------------------------------------------------------------------

@contextmanager
def offline_pipeline(eval_store: EvalStore, retriever: LexicalRetriever, *, model: str | None = None) -> Iterator[None]:
    """Point the pipeline at the stand-ins and restore everything afterwards."""
    saved_store = {name: getattr(store, name) for name in _STORE_FUNCTIONS}
    saved_search = rag.search
    saved_settings = (settings.alert_webhook_url, settings.triage_model)
    try:
        for name in _STORE_FUNCTIONS:
            setattr(store, name, getattr(eval_store, name))
        rag.search = retriever.search
        settings.alert_webhook_url = ""
        if model:
            settings.triage_model = model
        yield
    finally:
        for name, function in saved_store.items():
            setattr(store, name, function)
        rag.search = saved_search
        settings.alert_webhook_url, settings.triage_model = saved_settings


def _final(ticket: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": ticket["category"],
        "priority": ticket["priority"],
        "team": ticket["team"],
        "injection": bool(ticket["injection_suspected"]),
        "status": ticket["status"],
    }


def run_case(case: Case) -> dict[str, Any]:
    """Triage one case with whatever pipeline is currently configured and record the outcome."""
    record: dict[str, Any] = {
        "id": case.id,
        "subject": case.subject,
        "channel": case.channel,
        "tags": list(case.tags),
        "rationale": case.rationale,
        "expected": case.expected,
        "actual": None,
        "model_proposed": None,
        "rules_fired": [],
        "model": None,
        "fallback": False,
        "fallback_reason": None,
        "latency_ms": None,
        "pipeline_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "error": None,
    }
    inbound = InboundTicket(channel=case.channel, subject=case.subject, body=case.body, customer=case.customer)
    started = time.perf_counter()
    try:
        bundle = triage.process_ticket(None, inbound)
    except Exception as exc:  # A crash is a result to report, not a reason to stop the run.
        record["error"] = type(exc).__name__
        record["pipeline_ms"] = round((time.perf_counter() - started) * 1000)
        return record

    ticket, events = bundle.ticket, bundle.events
    triaged = next((e["detail"] for e in events if e["type"] == "triaged"), {})
    claude_error = next(
        (e["detail"].get("reason") for e in events if e["type"] == "error" and e["detail"].get("stage") == "claude"),
        None,
    )
    record.update(
        actual=_final(ticket),
        model_proposed={key: triaged.get(key) for key in ("category", "priority", "team")} if triaged else None,
        rules_fired=[
            e["detail"]["rule"]
            for e in events
            if e["type"] == "rule_applied" and e["detail"].get("rule") not in _BOOKKEEPING_RULES
        ],
        model=ticket["ai_model"],
        fallback=ticket["ai_model"] == triage.FALLBACK_MODEL,
        fallback_reason=claude_error,
        latency_ms=ticket["ai_latency_ms"],
        pipeline_ms=round((time.perf_counter() - started) * 1000),
        input_tokens=ticket["input_tokens"],
        output_tokens=ticket["output_tokens"],
    )
    return record


def run_cases(
    cases: Sequence[Case],
    *,
    model: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Run every case through the offline pipeline and return results in dataset order."""
    eval_store = EvalStore()
    with offline_pipeline(eval_store, LexicalRetriever(), model=model):
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            results = []
            for result in pool.map(run_case, cases):
                if progress:
                    progress(result)
                results.append(result)
    return results
