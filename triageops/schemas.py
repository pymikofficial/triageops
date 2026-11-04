"""Request bodies accepted by the API, with size limits enforced by pydantic.

``InboundTicket`` is the internal, channel neutral form every adapter produces
and the triage pipeline consumes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .taxonomy import Channel

MAX_CHAT_MESSAGES = 12
MAX_CHAT_MESSAGE_CHARS = 2000


@dataclass(frozen=True)
class InboundTicket:
    channel: str
    subject: str
    body: str
    customer: str | None = None
    requester: str | None = None
    requester_name: str | None = None
    client_hash: str | None = None
    is_demo: bool = False
    # Provider message id, so a retried delivery returns the original ticket.
    source_id: str | None = None
    # Extra keys for the "received" event, such as the RMM de-duplication key.
    source_detail: Mapping[str, Any] | None = None
    # A priority suggested by a verified integration (an RMM severity), shown to Claude as a hint.
    priority_hint: str | None = None
    # Why the hint was given, built from our own vocabulary, never from raw payload text.
    priority_hint_reason: str | None = None


class _Request(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


class TicketIn(_Request):
    customer: str | None = Field(default=None, max_length=120)
    requester: str | None = Field(default=None, max_length=160)
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=10, max_length=4000)

    @field_validator("customer", "requester")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value or None


class DemoTicketIn(TicketIn):
    channel: Literal["portal", "email", "whatsapp"] = "portal"
    # Honeypot. Hidden in the form, so only bots fill it in.
    website: str = Field(default="", max_length=200)


class InboundTicketIn(TicketIn):
    channel: Channel = "webhook"
    external_id: str | None = Field(default=None, max_length=120)


class PostmarkInboundIn(_Request):
    """The subset of a Postmark inbound email we use. Long emails are truncated, not rejected."""

    from_address: str = Field(default="", alias="From", max_length=320)
    from_name: str = Field(default="", alias="FromName", max_length=200)
    subject: str = Field(default="", alias="Subject", max_length=2000)
    text_body: str = Field(default="", alias="TextBody", max_length=200_000)
    mailbox_hash: str = Field(default="", alias="MailboxHash", max_length=120)
    message_id: str = Field(default="", alias="MessageID", max_length=200)


class ChatMessage(_Request):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_CHARS)


class ChatIn(_Request):
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_CHAT_MESSAGES)

    @model_validator(mode="after")
    def _ends_with_user(self) -> "ChatIn":
        if self.messages[-1].role != "user":
            raise ValueError("the last message must come from the user")
        return self
