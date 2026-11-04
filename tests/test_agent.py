"""The copilot tool-use loop, with a scripted fake Claude and faked tools."""

from types import SimpleNamespace

import pytest
from fakes import FakeAnthropic, text_response

from triageops import agent, tools
from triageops.config import settings
from triageops.schemas import ChatMessage


def tool_call(name: str, arguments: dict, call_id: str = "toolu_1") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="tool_use",
        model="claude-sonnet-5-5",
        content=[
            SimpleNamespace(type="text", text="Let me check the queue."),
            SimpleNamespace(type="tool_use", id=call_id, name=name, input=arguments),
        ],
    )


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def execute(name: str, arguments: dict) -> dict:
        calls.append((name, arguments))
        if name == "get_ticket":
            raise tools.ToolExecutionError("No ticket matches TKT-0000-0000")
        return {"count": 1, "tickets": [{"ref": "TKT-1104-4821", "priority": "P1"}]}

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(tools, "execute", execute)
    return calls


def ask(question: str) -> list[ChatMessage]:
    return [ChatMessage(role="assistant", content="Hello, ask me about the queue."), ChatMessage(role="user", content=question)]


def test_agent_runs_tools_then_answers(monkeypatch: pytest.MonkeyPatch, executed: list) -> None:
    fake = FakeAnthropic(
        tool_call("search_tickets", {"priority": "P1", "status": "escalated"}),
        text_response("One P1 is open, TKT-1104-4821."),
    )
    monkeypatch.setattr(agent, "_client", lambda key: fake)

    reply = agent.run_agent(ask("Which P1 tickets are open?"))

    assert reply.reply == "One P1 is open, TKT-1104-4821."
    assert reply.tools_used == [{"name": "search_tickets", "input": {"priority": "P1", "status": "escalated"}}]
    assert executed == [("search_tickets", {"priority": "P1", "status": "escalated"})]
    first, second = fake.requests
    assert first["messages"][0]["role"] == "user", "a leading assistant greeting is dropped"
    assert first["tool_choice"] == {"type": "auto"}
    assert "untrusted customer data" in first["system"]
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["is_error"] is False


def test_tool_errors_are_returned_to_the_model(monkeypatch: pytest.MonkeyPatch, executed: list) -> None:
    fake = FakeAnthropic(tool_call("get_ticket", {"ref": "TKT-0000-0000"}), text_response("I could not find that ticket."))
    monkeypatch.setattr(agent, "_client", lambda key: fake)

    agent.run_agent(ask("Show TKT-0000-0000"))

    tool_result = fake.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "No ticket matches" in tool_result["content"]


def test_the_last_turn_switches_tools_off(monkeypatch: pytest.MonkeyPatch, executed: list) -> None:
    looping = [tool_call("search_tickets", {}, call_id=f"toolu_{n}") for n in range(agent.MAX_TURNS - 1)]
    fake = FakeAnthropic(*looping, text_response("Here is what I found."))
    monkeypatch.setattr(agent, "_client", lambda key: fake)

    reply = agent.run_agent(ask("Keep searching"))

    assert len(fake.requests) == agent.MAX_TURNS
    assert fake.requests[-1]["tool_choice"] == {"type": "none"}
    assert reply.reply == "Here is what I found."
    assert len(reply.tools_used) == agent.MAX_TURNS - 1
