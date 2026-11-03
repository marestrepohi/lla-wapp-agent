"""Domain types shared by tools, workflows, and channel adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Intent(StrEnum):
    BALANCE = "balance"
    NEGOTIATE = "negotiate"
    CONFIRM_PROMISE = "confirm_promise"
    DISPUTE = "dispute"
    DISCOUNT = "discount"
    HUMAN = "human"
    OPT_OUT = "opt_out"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Balance:
    cliente_id: str
    amount_cents: int
    currency: str
    due_date: date | None
    status: str

    @property
    def amount_display(self) -> str:
        return f"{self.currency} {self.amount_cents / 100:,.2f}"


@dataclass(frozen=True)
class PaymentPromise:
    promise_id: str
    cliente_id: str
    amount_cents: int
    payment_date: date
    confirmed_at: datetime


@dataclass(frozen=True)
class Escalation:
    escalation_id: str
    cliente_id: str | None
    reason: str
    summary: str
    created_at: datetime


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, object]
    outcome: str


@dataclass(frozen=True)
class InboundMessage:
    """Normalized message and trusted channel context for one graph invocation."""

    text: str
    channel: str
    message_id: str
    external_id: str | None
    client_id: str | None
    conversation_id: str


@dataclass(frozen=True)
class AgentResponse:
    text: str
    intent: Intent
    tool_calls: list[ToolCall] = field(default_factory=list)
    escalated: bool = False
    opted_out: bool = False
    identity_validated: bool = False
    retrieved_documents: list[dict[str, object]] = field(default_factory=list)
