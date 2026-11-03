"""FastAPI/Mangum ingress that validates Meta and publishes messages to SQS FIFO."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from mangum import Mangum

_LOGGER = logging.getLogger(__name__)
_SECRET_ARN = os.getenv("WHATSAPP_SECRET_ARN", "").strip()
_LOCAL_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
_LOCAL_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "").strip()
_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "").strip()
_SQS = boto3.client("sqs")
_SECRETS = boto3.client("secretsmanager")

app = FastAPI(title="LLA AWS architecture demo")


@lru_cache(maxsize=1)
def _whatsapp_secrets() -> dict[str, str]:
    """Load the AWS secret once per Lambda execution environment, or local demo values."""
    if not _SECRET_ARN:
        return {
            "verify_token": _LOCAL_VERIFY_TOKEN,
            "app_secret": _LOCAL_APP_SECRET,
        }

    response = _SECRETS.get_secret_value(SecretId=_SECRET_ARN)
    secret_string = response.get("SecretString")
    if not isinstance(secret_string, str):
        raise ValueError("WhatsApp secret must contain a JSON SecretString")
    payload: Any = json.loads(secret_string)
    if not isinstance(payload, dict):
        raise ValueError("WhatsApp secret must contain a JSON object")
    return {
        "verify_token": str(payload.get("verify_token", "")).strip(),
        "app_secret": str(payload.get("app_secret", "")).strip(),
    }


def _secret_value(field: str) -> str:
    try:
        return _whatsapp_secrets().get(field, "")
    except (BotoCoreError, ClientError, ValueError, json.JSONDecodeError) as error:
        _LOGGER.error("WhatsApp secret lookup failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="Webhook secret unavailable") from None


def _inbound_messages(payload: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield sender, WhatsApp message ID, and message body from a Meta webhook payload."""
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


def _valid_meta_signature(raw_body: bytes, signature: str | None) -> bool:
    app_secret = _secret_value("app_secret")
    if (
        not app_secret
        or not signature
        or not signature.isascii()
        or not signature.startswith("sha256=")
    ):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _queue_ids(sender: str, message_id: str) -> tuple[str, str]:
    """Hash external identifiers for valid FIFO group and deduplication values."""
    group_id = hashlib.sha256(sender.encode("utf-8")).hexdigest()
    deduplication_id = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    return group_id, deduplication_id


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "architecture-demo"}


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Handle Meta's webhook verification challenge."""
    configured_token = _secret_value("verify_token")
    if (
        mode == "subscribe"
        and challenge is not None
        and verify_token is not None
        and verify_token.isascii()
        and configured_token.isascii()
        and configured_token
        and hmac.compare_digest(verify_token, configured_token)
    ):
        return PlainTextResponse(challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, int | bool]:
    """Verify Meta's signature and publish each inbound message to SQS FIFO."""
    raw_body = await request.body()
    if not _valid_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401, detail="Webhook signature invalid")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid webhook JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    messages = list(_inbound_messages(payload))
    if not messages:
        return {"received": True, "queued": 0}
    if not _QUEUE_URL:
        raise HTTPException(status_code=503, detail="SQS queue is not configured")

    try:
        for sender, message_id, message in messages:
            group_id, deduplication_id = _queue_ids(sender, message_id)
            envelope = {
                "message_id": message_id,
                "sender_id": sender,
                "message": message,
            }
            await asyncio.to_thread(
                _SQS.send_message,
                QueueUrl=_QUEUE_URL,
                MessageBody=json.dumps(envelope, ensure_ascii=False),
                MessageGroupId=group_id,
                MessageDeduplicationId=deduplication_id,
            )
    except (BotoCoreError, ClientError) as error:
        _LOGGER.error("SQS publish failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="Message queue unavailable") from None

    _LOGGER.info("Accepted WhatsApp webhook with %d message(s)", len(messages))
    return {"received": True, "queued": len(messages)}


handler = Mangum(app)
