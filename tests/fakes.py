"""In-memory stand-ins for the database layer and the Claude client. No network, no Postgres."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from triageops.store import TICKET_COLUMNS

WEBHOOK_SECRET = "test-webhook-secret"
MCP_TOKEN = "test-mcp-token"
CRON_SECRET = "test-cron-secret"
WHATSAPP_APP_SECRET = "test-whatsapp-app-secret"
WHATSAPP_VERIFY_TOKEN = "test-verify-token"

VALID_TRIAGE: dict[str, Any] = {
    "category": "Hardware",
    "priority": "P3",
    "sentiment": "neutral",
    "team": "Service Desk L1",
    "summary": "Front office printer shows offline on every laptop.",
    "suggested_reply": "Hello Sam,\n\nThank you for letting us know. We are checking the printer now.\n\nTriageOps Service Desk",
    "next_steps": ["Check the printer IP address", "Reserve a fixed address"],
    "kb_refs": [],
    "confidence": 0.86,
    "injection_suspected": False,
    "escalate": False,
    "escalation_reason": "",
}


class FakeStore:
    """Implements the ``triageops.store`` functions the pipeline and routes use."""

    def __init__(self) -> None:
        self.tickets: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.recent_from_client = 0
        self.demo_today = 0
        self.kb_articles = 12
        self.fail_update = False

    # Tickets
    def insert_ticket(self, conn: Any, **fields: Any) -> dict[str, Any]:
        self.calls.append("insert_ticket")
        ticket = dict.fromkeys(TICKET_COLUMNS)
        ticket.update(
            id=uuid.uuid4(),
            ref=f"TKT-1104-{len(self.tickets) + 1:04d}",
            created_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
            updated_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
            status="new",
            next_steps=[],
            kb_refs=[],
            sla_breached=False,
            injection_suspected=False,
            **fields,
        )
        ticket.pop("client_hash")
        self.tickets[str(ticket["id"])] = ticket
        return dict(ticket)

    def update_ticket(self, conn: Any, ticket_id: Any, fields: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("update_ticket")
        if self.fail_update:
            raise RuntimeError("simulated database failure")
        self.tickets[str(ticket_id)].update(fields)
        return dict(self.tickets[str(ticket_id)])

    def mark_failed(self, conn: Any, ticket_id: Any) -> None:
        self.tickets[str(ticket_id)]["status"] = "failed"

    def get_ticket(self, conn: Any, key: str) -> dict[str, Any] | None:
        for ticket in self.tickets.values():
            if key in (str(ticket["id"]), ticket["ref"]):
                return dict(ticket)
        return None

    def find_ticket_by_source(self, conn: Any, source_id: str) -> dict[str, Any] | None:
        for event in self.events:
            if event["type"] == "received" and event["detail"].get("source_id") == source_id:
                return dict(self.tickets[str(event["ticket_id"])])
        return None

    def count_recent_tickets_from(self, conn: Any, client_hash: str) -> int:
        return self.recent_from_client

    def count_demo_tickets_today(self, conn: Any) -> int:
        return self.demo_today

    # Events
    def add_events(self, conn: Any, ticket_id: Any, events: list[tuple[str, dict[str, Any]]]) -> None:
        for kind, detail in events:
            self.events.append(
                {
                    "id": len(self.events) + 1,
                    "ticket_id": ticket_id,
                    "created_at": datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
                    "type": kind,
                    "detail": dict(detail),
                }
            )

    def list_events(self, conn: Any, ticket_id: Any) -> list[dict[str, Any]]:
        return [
            {key: event[key] for key in ("id", "created_at", "type", "detail")}
            for event in self.events
            if event["ticket_id"] == ticket_id
        ]

    def event_types(self, ticket_id: Any) -> list[str]:
        return [event["type"] for event in self.list_events(None, ticket_id)]

    # Knowledge base
    def kb_count(self, conn: Any) -> int:
        return self.kb_articles


def tool_response(tool_input: dict[str, Any], *, stop_reason: str = "tool_use", name: str = "record_triage") -> Any:
    """A Messages API response shaped like the SDK's, containing one tool call."""
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-haiku-5-5",
        content=[SimpleNamespace(type="tool_use", id="toolu_01", name=name, input=tool_input)],
        usage=SimpleNamespace(input_tokens=1200, output_tokens=310),
    )


def text_response(text: str, model: str = "claude-sonnet-5-5") -> Any:
    return SimpleNamespace(
        stop_reason="end_turn",
        model=model,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=500, output_tokens=60),
    )


class FakeAnthropic:
    """Returns queued responses and records every ``messages.create`` call."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
