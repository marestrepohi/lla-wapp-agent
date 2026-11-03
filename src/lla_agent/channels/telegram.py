"""Optional Telegram transport adapter using the shared conversation runtime."""

from __future__ import annotations

import os

from lla_agent.application.ports import ConversationRuntime
from lla_agent.domain.models import AgentResponse, InboundMessage


class TelegramAdapter:
    """Translate trusted Telegram update metadata into a normalized event."""

    def __init__(self, runtime: ConversationRuntime):
        self.runtime = runtime

    async def handle_message(
        self,
        *,
        text: str,
        user_id: str,
        message_id: str,
        conversation_id: str,
        client_id: str | None = None,
    ) -> AgentResponse:
        return await self.runtime.handle(
            InboundMessage(
                text=text,
                channel="telegram",
                message_id=message_id,
                external_id=f"telegram:{user_id}",
                client_id=client_id,
                conversation_id=f"telegram:{conversation_id}:{user_id}",
            )
        )


def build_application(runtime: ConversationRuntime):
    """Build a Telegram application; the optional dependency loads only when called."""
    try:
        from telegram.ext import Application, MessageHandler, filters
    except ImportError as error:
        raise RuntimeError("Instala la dependencia opcional: uv sync --extra telegram") from error

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Define TELEGRAM_BOT_TOKEN fuera del código fuente")

    adapter = TelegramAdapter(runtime)
    application = Application.builder().token(token).build()

    async def on_message(update, context):
        if not update.message or not update.message.text or not update.effective_user:
            return
        chat = update.effective_chat
        conversation_id = str(chat.id if chat else update.effective_user.id)
        response = await adapter.handle_message(
            text=update.message.text,
            user_id=str(update.effective_user.id),
            message_id=f"telegram:{update.update_id}",
            conversation_id=conversation_id,
        )
        await update.message.reply_text(response.text)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return application
