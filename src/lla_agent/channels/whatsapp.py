"""Local adapter from verified WhatsApp events to the shared runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from lla_agent.application.ports import ConversationRuntime
from lla_agent.domain.models import AgentResponse, InboundMessage


@dataclass(frozen=True)
class WhatsAppMessage:
    message_id: str
    sender_id: str
    text: str
    opening_context: Mapping[str, str] | None = None


class WhatsAppAdapter:
    """Normalize a Meta event already verified by the eventual webhook boundary."""

    def __init__(self, runtime: ConversationRuntime):
        self.runtime = runtime

    async def handle_verified_message(
        self, message: WhatsAppMessage, client_id: str | None = None
    ) -> AgentResponse:
        """Pass trusted channel identity and text to the common agent runtime."""
        return await self.runtime.handle(
            InboundMessage(
                text=message.text,
                channel="whatsapp",
                message_id=message.message_id,
                external_id=f"whatsapp:{message.sender_id}",
                client_id=client_id,
                conversation_id=f"whatsapp:{message.sender_id}",
                opening_context=message.opening_context,
            )
        )
