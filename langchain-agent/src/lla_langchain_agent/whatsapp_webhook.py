"""Local Meta WhatsApp webhook backed by the shared LangChain runtime."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from lla_agent.channels.whatsapp import WhatsAppAdapter, WhatsAppMessage
from lla_agent.persistence.sqlite import SQLiteRepository

from .agent import DEFAULT_MODEL
from .runtime import AgentRuntime, open_runtime
from .whatsapp_media import InboundMediaError, normalize_whatsapp_message

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = Path(__file__).resolve().parents[2]
load_dotenv(APP_DIR / ".env")

_LOGGER = logging.getLogger(__name__)
_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
_ACCESS_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip() or os.getenv(
    "WHATSAPP_ACCESS_TOKEN", ""
).strip()
_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
_GRAPH_API_VERSION = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0").strip()
_QA_ID_LINKING = os.getenv("WHATSAPP_QA_ID_LINKING", "false").strip().casefold() in {
    "1", "true", "yes", "on"
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if not _VERIFY_TOKEN:
        raise RuntimeError("Configura WHATSAPP_VERIFY_TOKEN en langchain-agent/.env.")
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("Configura GROQ_API_KEY en langchain-agent/.env.")

    database_path = Path(
        os.getenv("LLA_DB_PATH", str(PROJECT_ROOT / "data" / "collections.sqlite3"))
    )
    checkpoint_path = APP_DIR / "whatsapp-checkpoints.sqlite"
    repository = SQLiteRepository(database_path)
    async with open_runtime(
        repository=repository,
        checkpoint_path=checkpoint_path,
        model_name=os.getenv("GROQ_MODEL", DEFAULT_MODEL),
        qa_id_linking=_QA_ID_LINKING,
    ) as runtime:
        app.state.runtime = runtime
        app.state.seen_message_ids = set()
        app.state.seen_lock = asyncio.Lock()
        _LOGGER.info("Local WhatsApp webhook ready")
        yield


app = FastAPI(title="LLA WhatsApp webhook", lifespan=_lifespan)


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Complete Meta's one-time GET challenge handshake."""
    if (
        mode == "subscribe"
        and challenge is not None
        and verify_token is not None
        and hmac.compare_digest(verify_token, _VERIFY_TOKEN)
    ):
        return PlainTextResponse(challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


def _inbound_messages(payload: dict[str, Any]):
    for entry in payload.get("entry", []):
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []):
            if not isinstance(change, dict):
                continue
            value = change.get("value", {})
            if not isinstance(value, dict):
                continue
            for message in value.get("messages", []):
                if not isinstance(message, dict):
                    continue
                sender = message.get("from")
                message_id = message.get("id")
                if all(isinstance(item, str) and item for item in (sender, message_id)):
                    yield sender, message_id, message


async def _send_text_reply(recipient: str, text: str) -> None:
    if not _ACCESS_TOKEN or not _PHONE_NUMBER_ID:
        _LOGGER.warning(
            "WhatsApp reply was generated but not sent; configure WHATSAPP_TOKEN "
            "and WHATSAPP_PHONE_NUMBER_ID."
        )
        return

    url = (
        f"https://graph.facebook.com/{_GRAPH_API_VERSION}/"
        f"{_PHONE_NUMBER_ID}/messages"
    )
    headers = {"Authorization": f"Bearer {_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPError as error:
        _LOGGER.error("WhatsApp send failed (%s).", type(error).__name__)
        return
    _LOGGER.info("WhatsApp reply sent.")


async def _process_message(
    runtime: AgentRuntime, sender: str, message_id: str, payload: dict[str, Any]
) -> None:
    adapter = WhatsAppAdapter(runtime)
    try:
        text = await normalize_whatsapp_message(payload)
        result = await adapter.handle_verified_message(
            WhatsAppMessage(message_id=message_id, sender_id=sender, text=text),
        )
        await _send_text_reply(sender, result.text)
    except InboundMediaError as error:
        _LOGGER.warning("WhatsApp media input could not be processed.")
        await _send_text_reply(sender, error.reply_text)
    except Exception as error:
        _LOGGER.error("WhatsApp message processing failed (%s).", type(error).__name__)


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
    """Accept local development webhooks and enqueue supported messages."""
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    runtime: AgentRuntime = request.app.state.runtime
    seen: set[str] = request.app.state.seen_message_ids
    lock: asyncio.Lock = request.app.state.seen_lock
    queued = 0
    async with lock:
        for sender, message_id, message in _inbound_messages(payload):
            if message_id in seen:
                continue
            seen.add(message_id)
            if len(seen) > 10000:
                seen.clear()
                seen.add(message_id)
            background_tasks.add_task(_process_message, runtime, sender, message_id, message)
            queued += 1
    _LOGGER.info("Accepted WhatsApp webhook with %d inbound message(s).", queued)
    return {"received": True}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
