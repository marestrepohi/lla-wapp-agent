"""Idempotent SQS worker for the WhatsApp LangGraph agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any

import boto3
import httpx

from lla_agent.channels.whatsapp import WhatsAppAdapter, WhatsAppMessage
from lla_agent.domain.models import InboundMessage
from lla_langchain_agent.whatsapp_media import InboundMediaError, normalize_whatsapp_message

from .agent_runtime import (
    SecretConfigurationError,
    SecretUnavailableError,
    configure_whatsapp_token,
    open_aws_runtime,
)
from .message_store import DynamoDBMessageStore

_LOGGER = logging.getLogger(__name__)
_EVENT_LOOP: asyncio.AbstractEventLoop | None = None
_RUNTIME_CONTEXT: Any | None = None
_RUNTIME: Any | None = None
_MESSAGE_STORE: Any | None = None
_SQS = boto3.client("sqs")
_PROCESSING_LEASE_SECONDS = 180
_VISIBILITY_DELAYS = (15, 30, 60, 120, 240)


class RetryableMessageError(RuntimeError):
    """A failed record that can be retried without risking a duplicate reply."""

    def __init__(self, category: str, *, status_code: int | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.status_code = status_code


class PermanentMessageError(RuntimeError):
    """A known non-retryable event that stays locked until operator recovery."""

    def __init__(self, category: str, *, status_code: int | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.status_code = status_code


class AmbiguousDeliveryError(RuntimeError):
    """A send may have been accepted, so it must not be repeated automatically."""

    def __init__(self, category: str, *, status_code: int | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.status_code = status_code


class MetaRateLimitError(RuntimeError):
    """Meta definitively rejected the send because of a rate limit."""

    def __init__(self, status_code: int = 429) -> None:
        super().__init__("meta_rate_limited")
        self.status_code = status_code


class MetaPermanentError(RuntimeError):
    """Meta returned a non-retryable client error for the send."""

    def __init__(self, status_code: int | None) -> None:
        super().__init__("meta_permanent_rejection")
        self.status_code = status_code


class MetaAmbiguousError(RuntimeError):
    """The Meta request may have reached the provider without a confirmed result."""

    def __init__(self, category: str, *, status_code: int | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.status_code = status_code


class PoisonEnvelopeError(ValueError):
    """An SQS envelope cannot be safely attributed to its FIFO sender group."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required Lambda configuration: {name}")
    return value


def _message_hash(message_id: str) -> str:
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]


def _log_event(event_name: str, *, message_hash: str, receive_count: int, **fields: Any) -> None:
    payload = {
        "event": event_name,
        "message_hash": message_hash,
        "receive_count": receive_count,
        **fields,
    }
    _LOGGER.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _safe_error_context(error: Exception) -> tuple[int | None, str]:
    """Extract only an HTTP status and host; never include response bodies or URLs."""
    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    request = getattr(error, "request", None) or getattr(response, "request", None)
    url = getattr(request, "url", None)
    host = getattr(url, "host", None)
    return (status if isinstance(status, int) else None, str(host or "-")[:128])


def _get_message_store() -> Any:
    """Open the idempotency store before loading Groq/Qdrant or the agent graph."""
    global _MESSAGE_STORE
    if _MESSAGE_STORE is None:
        _MESSAGE_STORE = DynamoDBMessageStore(_required_env("DYNAMODB_CONVERSATION_TABLE"))
    return _MESSAGE_STORE


async def _get_runtime() -> Any:
    global _RUNTIME_CONTEXT, _RUNTIME, _MESSAGE_STORE
    if _RUNTIME is None:
        _RUNTIME_CONTEXT, _RUNTIME, runtime_message_store = await open_aws_runtime()
        if _MESSAGE_STORE is None:
            _MESSAGE_STORE = runtime_message_store
    return _RUNTIME


async def _send_text_reply(recipient: str, text: str) -> str:
    """Send one reply and return the outbound Meta message ID."""
    token = os.getenv("WHATSAPP_TOKEN", "").strip()
    try:
        phone_number_id = _required_env("WHATSAPP_PHONE_NUMBER_ID")
    except RuntimeError as error:
        raise MetaPermanentError(None) from error
    api_version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0").strip()
    if not token:
        raise MetaPermanentError(None)

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": text[:4096]},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as error:
        raise RetryableMessageError("meta_connection_not_established") from error
    except httpx.HTTPError as error:
        raise MetaAmbiguousError("meta_transport_after_send_started") from error

    status = response.status_code
    if status == 429:
        raise MetaRateLimitError(status)
    if status == 408 or status >= 500:
        raise MetaAmbiguousError("meta_response_delivery_unknown", status_code=status)
    if status >= 300 or status < 200:
        raise MetaPermanentError(status)

    try:
        payload_response: Any = response.json()
    except (ValueError, TypeError) as error:
        raise MetaAmbiguousError("meta_success_response_invalid", status_code=status) from error
    messages = payload_response.get("messages") if isinstance(payload_response, dict) else None
    provider_message_id = (
        messages[0].get("id")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict)
        else None
    )
    if not isinstance(provider_message_id, str) or not provider_message_id:
        raise MetaAmbiguousError("meta_success_response_missing_id", status_code=status)
    return provider_message_id


def _decode_envelope(
    body: str, *, group_id_hash: str | None
) -> tuple[str, str, dict[str, Any]]:
    try:
        envelope: Any = json.loads(body)
    except json.JSONDecodeError as error:
        raise PoisonEnvelopeError("invalid_json_envelope") from error
    if not isinstance(envelope, dict):
        raise PoisonEnvelopeError("invalid_envelope_shape")
    sender = envelope.get("sender_id")
    message_id = envelope.get("message_id")
    message = envelope.get("message")
    if not (
        isinstance(sender, str)
        and sender
        and isinstance(message_id, str)
        and message_id
        and isinstance(message, dict)
    ):
        raise PoisonEnvelopeError("missing_sender_message_id_or_payload")
    derived_group_hash = hashlib.sha256(sender.encode("utf-8")).hexdigest()
    if group_id_hash != derived_group_hash:
        raise PoisonEnvelopeError("message_group_sender_mismatch")
    return sender, message_id, message


def _agent_error_status(error: Exception) -> int | None:
    status, _host = _safe_error_context(error)
    return status


def _agent_error_is_permanent(error: Exception) -> bool:
    status = _agent_error_status(error)
    if status is not None:
        return 400 <= status < 500 and status not in {408, 429}
    return isinstance(error, (ValueError, TypeError, KeyError))


def _media_audit_placeholder(payload: dict[str, Any]) -> str:
    """Describe an unprocessable attachment without persisting its URL or media ID."""
    message_type = payload.get("type")
    if message_type == "audio":
        return "[audio adjunto]"
    if message_type == "image":
        return "[imagen adjunta]"
    return "[adjunto no procesado]"


async def _make_reply(
    runtime: Any, sender: str, message_id: str, payload: dict[str, Any]
) -> tuple[str, str]:
    try:
        try:
            text = await normalize_whatsapp_message(payload)
        except InboundMediaError as error:
            runtime.record_direct_exchange(
                InboundMessage(
                    text=_media_audit_placeholder(payload),
                    channel="whatsapp",
                    message_id=message_id,
                    external_id=f"whatsapp:{sender}",
                    client_id=None,
                    conversation_id=f"whatsapp:{sender}",
                ),
                error.reply_text,
            )
            return error.reply_text, "media_error"

        adapter = WhatsAppAdapter(runtime)
        result = await adapter.handle_verified_message(
            WhatsAppMessage(message_id=message_id, sender_id=sender, text=text)
        )
        return result.text, "generated"
    except Exception as error:
        if _agent_error_is_permanent(error):
            raise PermanentMessageError(
                "agent_permanent_error", status_code=_agent_error_status(error)
            ) from error
        raise RetryableMessageError(
            "agent_processing_error", status_code=_agent_error_status(error)
        ) from error


async def _process_record(
    body: str,
    *,
    sqs_message_id: str,
    group_id_hash: str | None,
    receive_count: int,
) -> tuple[str, str]:
    processing_started = time.monotonic()
    message_store = _get_message_store()
    try:
        sender, message_id, payload = _decode_envelope(body, group_id_hash=group_id_hash)
    except PoisonEnvelopeError as error:
        message_store.quarantine_poison(group_id_hash or "", sqs_message_id)
        raise PermanentMessageError(error.category) from error

    message_hash = _message_hash(message_id)
    _log_event(
        "queue_message_processing_started",
        message_hash=message_hash,
        receive_count=receive_count,
    )
    claim = message_store.claim(sender, message_id, _PROCESSING_LEASE_SECONDS)

    if claim.status == "already_sent":
        return _message_hash(message_id), "already_sent"
    if claim.status in {"in_progress", "conversation_paused"}:
        raise RetryableMessageError(claim.status)
    if claim.status == "failed":
        raise PermanentMessageError("message_previously_failed")
    if claim.status == "send_unknown":
        raise AmbiguousDeliveryError("message_delivery_unknown")

    if claim.status == "reply_ready":
        reply_text = claim.response_text
        if not isinstance(reply_text, str):
            raise PermanentMessageError("saved_reply_missing")
        reply_source = "reused"
    elif claim.status == "claimed":
        try:
            runtime = await _get_runtime()
        except SecretUnavailableError as error:
            raise RetryableMessageError("agent_secret_temporarily_unavailable") from error
        except SecretConfigurationError as error:
            message_store.mark_failed(sender, message_id, "invalid_agent_secret")
            raise PermanentMessageError("invalid_agent_secret") from error
        try:
            reply_text, reply_source = await _make_reply(runtime, sender, message_id, payload)
        except PermanentMessageError as error:
            message_store.mark_failed(sender, message_id, error.category)
            raise
        message_store.save_reply(sender, message_id, reply_text)
    else:
        raise PermanentMessageError("unknown_message_state")

    _log_event(
        "agent_complete",
        message_hash=message_hash,
        receive_count=receive_count,
        reply_source=reply_source,
        response_state="persisted_or_reused",
        elapsed_ms=int((time.monotonic() - processing_started) * 1000),
    )

    try:
        configure_whatsapp_token()
    except SecretUnavailableError as error:
        raise RetryableMessageError("whatsapp_secret_temporarily_unavailable") from error
    except SecretConfigurationError as error:
        message_store.mark_failed(sender, message_id, "invalid_whatsapp_secret")
        raise PermanentMessageError("invalid_whatsapp_secret") from error

    message_store.mark_sending(sender, message_id)
    _log_event(
        "whatsapp_send_started",
        message_hash=message_hash,
        receive_count=receive_count,
        elapsed_ms=int((time.monotonic() - processing_started) * 1000),
    )
    try:
        provider_message_id = await _send_text_reply(sender, reply_text)
    except MetaRateLimitError as error:
        message_store.mark_retryable(sender, message_id)
        raise RetryableMessageError("meta_rate_limited", status_code=error.status_code) from error
    except RetryableMessageError:
        # The connection was not established, so Meta could not have accepted the request.
        message_store.mark_retryable(sender, message_id)
        raise
    except MetaPermanentError as error:
        message_store.mark_failed(sender, message_id, "meta_permanent_rejection")
        raise PermanentMessageError(
            "meta_permanent_rejection", status_code=error.status_code
        ) from error
    except MetaAmbiguousError as error:
        message_store.mark_send_unknown(sender, message_id)
        raise AmbiguousDeliveryError(
            error.category, status_code=error.status_code
        ) from error

    _log_event(
        "whatsapp_send_accepted",
        message_hash=message_hash,
        receive_count=receive_count,
        provider_status="accepted",
        elapsed_ms=int((time.monotonic() - processing_started) * 1000),
    )
    message_store.mark_sent(sender, message_id, provider_message_id)
    return message_hash, "sent"


def _receive_count(record: dict[str, Any]) -> int:
    attributes = record.get("attributes")
    value = attributes.get("ApproximateReceiveCount") if isinstance(attributes, dict) else None
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return 1


def _visibility_delay(receive_count: int) -> int:
    return _VISIBILITY_DELAYS[min(max(receive_count - 1, 0), len(_VISIBILITY_DELAYS) - 1)]


def _change_message_visibility(record: dict[str, Any], timeout: int) -> None:
    queue_url = _required_env("SQS_QUEUE_URL")
    receipt_handle = record.get("receiptHandle")
    if not isinstance(receipt_handle, str) or not receipt_handle:
        raise RuntimeError("SQS receipt handle is unavailable")
    _SQS.change_message_visibility(
        QueueUrl=queue_url,
        ReceiptHandle=receipt_handle,
        VisibilityTimeout=timeout,
    )


def _failed_ids_from(records: list[Any], start: int) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for record in records[start:]:
        if isinstance(record, dict) and isinstance(record.get("messageId"), str):
            failures.append({"itemIdentifier": record["messageId"]})
    return failures


async def _handle_event(event: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    records = event.get("Records", [])
    if not isinstance(records, list):
        raise ValueError("Lambda SQS event has invalid Records")

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            # With batch size 1 this branch is only a malformed Lambda envelope. Raising causes
            # Lambda to retry the batch rather than silently acknowledging a record we cannot name.
            raise ValueError("Lambda SQS record is malformed")
        item_identifier = record.get("messageId")
        body = record.get("body")
        if not isinstance(item_identifier, str) or not isinstance(body, str):
            raise ValueError("Lambda SQS record is missing messageId or body")

        receive_count = _receive_count(record)
        message_hash = _message_hash(item_identifier)
        attributes = record.get("attributes")
        raw_group_id = attributes.get("MessageGroupId") if isinstance(attributes, dict) else None
        group_id_hash = raw_group_id if isinstance(raw_group_id, str) else None
        started = time.monotonic()
        try:
            message_hash, status = await _process_record(
                body,
                sqs_message_id=item_identifier,
                group_id_hash=group_id_hash,
                receive_count=receive_count,
            )
        except RetryableMessageError as error:
            status_code, host = _safe_error_context(error)
            delay = _visibility_delay(receive_count)
            failures = _failed_ids_from(records, index)
            _log_event(
                "queue_message_failed",
                message_hash=message_hash,
                receive_count=receive_count,
                category=error.category,
                retryable=True,
                http_status=error.status_code or status_code,
                upstream=host,
                retry_delay_seconds=delay,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            # Keep this as the final external operation before returning the partial-batch failure.
            try:
                _change_message_visibility(record, delay)
            except Exception:
                # The queue's configured visibility remains the fallback; don't mask the retry.
                pass
            return {"batchItemFailures": failures}
        except (PermanentMessageError, AmbiguousDeliveryError) as error:
            status_code, host = _safe_error_context(error)
            category = getattr(error, "category", "delivery_unknown")
            _log_event(
                "queue_message_failed",
                message_hash=message_hash,
                receive_count=receive_count,
                category=category,
                retryable=False,
                delivery_unknown=isinstance(error, AmbiguousDeliveryError),
                http_status=getattr(error, "status_code", None) or status_code,
                upstream=host,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            return {"batchItemFailures": _failed_ids_from(records, index)}
        except Exception as error:
            status_code, host = _safe_error_context(error)
            delay = _visibility_delay(receive_count)
            failures = _failed_ids_from(records, index)
            _log_event(
                "queue_message_failed",
                message_hash=message_hash,
                receive_count=receive_count,
                category="unexpected_processing_error",
                retryable=True,
                error_type=type(error).__name__,
                http_status=status_code,
                upstream=host,
                retry_delay_seconds=delay,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            try:
                _change_message_visibility(record, delay)
            except Exception:
                pass
            return {"batchItemFailures": failures}

        _log_event(
            "queue_message_processed",
            message_hash=message_hash,
            receive_count=receive_count,
            status=status,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    return {"batchItemFailures": []}


def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    """Run SQS records and retry the failed FIFO record and all records behind it."""
    del context
    global _EVENT_LOOP
    if _EVENT_LOOP is None or _EVENT_LOOP.is_closed():
        _EVENT_LOOP = asyncio.new_event_loop()
    return _EVENT_LOOP.run_until_complete(_handle_event(event))
