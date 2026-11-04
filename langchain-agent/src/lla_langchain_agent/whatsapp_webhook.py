"""Local Meta WhatsApp webhook backed by the shared LangChain runtime."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
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
from .rag import DEFAULT_EMBEDDING_MODEL, WebCorpus
from .models import chat_credentials_available
from .runtime import AgentRuntime, open_runtime
from .whatsapp_media import InboundMediaError, normalize_whatsapp_message
from .whatsapp_opening import parse_opening_callback_data

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = Path(__file__).resolve().parents[2]
load_dotenv(APP_DIR / ".env")

_LOGGER = logging.getLogger(__name__)
_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
_ACCESS_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip() or os.getenv(
    "WHATSAPP_ACCESS_TOKEN", ""
).strip()
_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
_GRAPH_API_VERSION = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v25.0").strip()
_QA_ID_LINKING = os.getenv("WHATSAPP_QA_ID_LINKING", "false").strip().casefold() in {
    "1", "true", "yes", "on"
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if not _VERIFY_TOKEN:
        raise RuntimeError("Configura WHATSAPP_VERIFY_TOKEN en langchain-agent/.env.")
    if not chat_credentials_available():
        raise RuntimeError("Configura GROQ_API_KEY en langchain-agent/.env.")
    if not os.getenv("GROQ_API_KEY", "").strip():
        _LOGGER.warning("Audio e imágenes de WhatsApp requieren GROQ_API_KEY.")

    database_path = Path(
        os.getenv("LLA_DB_PATH", str(PROJECT_ROOT / "data" / "collections.sqlite3"))
    )
    state_dir = Path(os.getenv("LLA_STATE_DIR", str(APP_DIR)))
    checkpoint_path = Path(
        os.getenv(
            "LLA_WHATSAPP_CHECKPOINT_PATH",
            str(state_dir / "whatsapp-checkpoints.sqlite"),
        )
    )
    web_corpus = WebCorpus(
        persist_directory=Path(
            os.getenv("LLA_VECTOR_STORE_PATH", str(state_dir / "chroma"))
        ),
        embedding_model=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
    )
    repository = SQLiteRepository(database_path)
    async with open_runtime(
        repository=repository,
        checkpoint_path=checkpoint_path,
        model_name=os.getenv("GROQ_MODEL", DEFAULT_MODEL),
        web_corpus=web_corpus,
        qa_id_linking=_QA_ID_LINKING,
    ) as runtime:
        app.state.runtime = runtime
        app.state.repository = repository
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


def _message_status_updates(payload: dict[str, Any]):
    for entry in payload.get("entry", []):
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []):
            if not isinstance(change, dict):
                continue
            value = change.get("value", {})
            if not isinstance(value, dict):
                continue
            for status_item in value.get("statuses", []):
                if not isinstance(status_item, dict):
                    continue
                message_id = status_item.get("id")
                status = status_item.get("status")
                timestamp = status_item.get("timestamp")
                recipient_id = status_item.get("recipient_id")
                callback_data = status_item.get("biz_opaque_callback_data")
                if (
                    isinstance(message_id, str)
                    and message_id
                    and isinstance(status, str)
                    and status in {"sent", "delivered", "read", "failed"}
                ):
                    yield (
                        message_id,
                        status,
                        timestamp if isinstance(timestamp, str) else None,
                        recipient_id
                        if isinstance(recipient_id, str)
                        and 0 < len(recipient_id) <= 128
                        and not any(ord(char) < 32 for char in recipient_id)
                        else None,
                        callback_data if isinstance(callback_data, str) else None,
                    )


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
    runtime: AgentRuntime,
    repository: SQLiteRepository,
    sender: str,
    message_id: str,
    payload: dict[str, Any],
) -> None:
    adapter = WhatsAppAdapter(runtime)
    started = time.perf_counter()
    try:
        text = await normalize_whatsapp_message(payload)
        normalized_at = time.perf_counter()
        callback_data = repository.consume_whatsapp_opening_context(f"whatsapp:{sender}")
        opening_context = (
            parse_opening_callback_data(callback_data) if callback_data is not None else None
        )
        result = await adapter.handle_verified_message(
            WhatsAppMessage(
                message_id=message_id,
                sender_id=sender,
                text=text,
                opening_context=opening_context,
            ),
        )
        completed_at = time.perf_counter()
        await _send_text_reply(sender, result.text)
        finished_at = time.perf_counter()
        _LOGGER.info(
            "Processed WhatsApp turn (normalize=%dms runtime=%dms send=%dms total=%dms).",
            round((normalized_at - started) * 1000),
            round((completed_at - normalized_at) * 1000),
            round((finished_at - completed_at) * 1000),
            round((finished_at - started) * 1000),
        )
    except InboundMediaError as error:
        _LOGGER.warning("WhatsApp media input could not be processed.")
        await _send_text_reply(sender, error.reply_text)
    except Exception as error:
        _LOGGER.error(
            "WhatsApp message processing failed (%s) after %dms.",
            type(error).__name__,
            round((time.perf_counter() - started) * 1000),
        )


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
    repository: SQLiteRepository = request.app.state.repository
    for message_id, status, timestamp, recipient_id, callback_data in _message_status_updates(
        payload
    ):
        opening_context = (
            parse_opening_callback_data(callback_data)
            if callback_data is not None and status != "failed"
            else None
        )
        normalized_callback = (
            json.dumps(opening_context, ensure_ascii=False, separators=(",", ":"))
            if opening_context is not None
            else None
        )
        repository.record_message_status(
            message_id,
            status,
            timestamp,
            recipient_id=recipient_id if normalized_callback else None,
            callback_data=normalized_callback,
        )

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
            background_tasks.add_task(
                _process_message, runtime, repository, sender, message_id, message
            )
            queued += 1
    _LOGGER.info("Accepted WhatsApp webhook with %d inbound message(s).", queued)
    return {"received": True}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
