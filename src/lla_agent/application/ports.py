"""Application contracts that keep channel adapters independent of LangGraph."""

from __future__ import annotations

from typing import Protocol

from lla_agent.domain.models import AgentResponse, InboundMessage


class ConversationRuntime(Protocol):
    """Port implemented by the LangGraph runtime and consumed by channel adapters."""

    async def handle(self, message: InboundMessage) -> AgentResponse:
        """Process one normalized inbound event and return its response."""
