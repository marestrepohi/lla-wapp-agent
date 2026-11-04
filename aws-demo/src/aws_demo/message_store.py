"""Durable WhatsApp message claims and per-sender turn ordering in DynamoDB."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config

_TTL_SECONDS = 90 * 24 * 60 * 60
_DEFAULT_SEND_LEASE_SECONDS = 180

ClaimStatus = Literal[
    "claimed",
    "reply_ready",
    "already_sent",
    "in_progress",
    "send_unknown",
    "failed",
    "conversation_paused",
]


@dataclass(frozen=True, slots=True)
class MessageClaim:
    """Result of claiming an inbound Meta message for processing."""

    status: ClaimStatus
    response_text: str | None = None


class MessageStoreConflictError(RuntimeError):
    """Raised when an update conflicts with the durable message state."""


class DynamoDBMessageStore:
    """Store idempotency/outbox state and serialize turns by WhatsApp sender.

    Event rows expire after 90 days. Sender lock rows intentionally have no TTL and
    remain active until a successful Meta send is committed as ``SENT``.
    """

    def __init__(
        self,
        table_name: str,
        *,
        dynamodb: Any | None = None,
        config: Config | None = None,
        send_lease_seconds: int = _DEFAULT_SEND_LEASE_SECONDS,
    ) -> None:
        if not table_name.strip():
            raise ValueError("DYNAMODB_CONVERSATION_TABLE debe tener un nombre.")
        if send_lease_seconds <= 0:
            raise ValueError("send_lease_seconds debe ser positivo.")
        resource = dynamodb or boto3.resource("dynamodb", config=config)
        self.table = resource.Table(table_name)
        # The DynamoDB resource's client keeps native Python values auto-marshalled.
        self._client = self.table.meta.client
        self._send_lease_seconds = send_lease_seconds

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()

    @classmethod
    def _event_key(cls, message_id: str) -> dict[str, str]:
        return {"pk": f"INBOUND#{cls._digest(message_id)}", "sk": "STATE"}

    @classmethod
    def _sender_key(cls, sender_id: str) -> dict[str, str]:
        return cls._sender_key_from_hash(cls._digest(sender_id))

    @staticmethod
    def _sender_key_from_hash(sender_hash: str) -> dict[str, str]:
        return {"pk": f"CONVERSATION#{sender_hash}", "sk": "ACTIVE_TURN"}

    @staticmethod
    def _valid_group_hash(group_id_hash: str) -> bool:
        return re.fullmatch(r"[0-9a-f]{64}", group_id_hash) is not None

    def _get(self, key: dict[str, str]) -> dict[str, Any] | None:
        response = self.table.get_item(Key=key, ConsistentRead=True)
        return response.get("Item")

    def _transact(self, transact_items: list[dict[str, Any]]) -> None:
        self._client.transact_write_items(TransactItems=transact_items)

    def quarantine_poison(self, group_id_hash: str, sqs_message_id: str) -> None:
        """Persist a malformed SQS turn without storing its raw sender or queue ID.

        If an older active turn already owns the sender lock, preserve it. That lock blocks
        this FIFO group until the older turn is resolved; recovery redrives the group's DLQ
        messages in FIFO order.
        """
        if not self._valid_group_hash(group_id_hash):
            raise ValueError("MessageGroupId no es un hash SHA-256 válido.")
        if not isinstance(sqs_message_id, str) or not sqs_message_id.strip():
            raise ValueError("SQS message ID es obligatorio.")

        poison_hash = self._digest(sqs_message_id)
        event_key = {"pk": f"POISON#{poison_hash}", "sk": "STATE"}
        sender_key = self._sender_key_from_hash(group_id_hash)
        now = int(time.time())
        poison_event = {
            **event_key,
            "item_type": "poison_message",
            "message_id_hash": poison_hash,
            "sender_id_hash": group_id_hash,
            "status": "FAILED",
            "failure_category": "invalid_queue_envelope",
            "created_at": now,
            "updated_at": now,
            "expires_at": now + _TTL_SECONDS,
        }

        for _attempt in range(5):
            existing_event = self._get(event_key)
            if existing_event and (
                existing_event.get("sender_id_hash") != group_id_hash
                or existing_event.get("message_id_hash") != poison_hash
                or existing_event.get("status") != "FAILED"
            ):
                raise MessageStoreConflictError("El mensaje inválido ya está asociado a otro estado.")

            sender = self._get(sender_key)
            active_message_id = (sender or {}).get("active_message_id")
            if active_message_id == poison_hash:
                if existing_event:
                    return
                continue
            if existing_event and active_message_id:
                # The poison row is already persisted and an older turn still blocks this sender.
                return

            put_or_check_event: dict[str, Any]
            if existing_event:
                put_or_check_event = {
                    "ConditionCheck": {
                        "TableName": self.table.name,
                        "Key": event_key,
                        "ConditionExpression": (
                            "#status = :failed AND sender_id_hash = :sender_hash "
                            "AND message_id_hash = :poison_hash"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":failed": "FAILED",
                            ":sender_hash": group_id_hash,
                            ":poison_hash": poison_hash,
                        },
                    }
                }
            else:
                put_or_check_event = {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": poison_event,
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }

            if active_message_id:
                # Preserve an older active turn; only assert that it remains current while
                # the poison ledger item is created.
                lock_operation = {
                    "ConditionCheck": {
                        "TableName": self.table.name,
                        "Key": sender_key,
                        "ConditionExpression": "active_message_id = :active",
                        "ExpressionAttributeValues": {":active": active_message_id},
                    }
                }
            else:
                lock_operation = {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": sender_key,
                        "UpdateExpression": "SET active_message_id = :poison_hash, locked_at = :now",
                        "ConditionExpression": "attribute_not_exists(active_message_id)",
                        "ExpressionAttributeValues": {
                            ":poison_hash": poison_hash,
                            ":now": now,
                        },
                    }
                }

            try:
                self._transact([lock_operation, put_or_check_event])
                return
            except self._client.exceptions.TransactionCanceledException:
                # A concurrent delivery may have created the exact same poison row/lock.
                continue

        existing_event = self._get(event_key)
        sender = self._get(sender_key)
        if (
            existing_event
            and existing_event.get("sender_id_hash") == group_id_hash
            and existing_event.get("status") == "FAILED"
            and (sender or {}).get("active_message_id")
        ):
            return
        raise MessageStoreConflictError("No se pudo bloquear el remitente por mensaje inválido.")

    def _new_event(
        self,
        *,
        sender_id: str,
        message_id: str,
        now: int,
        lease_until: int,
    ) -> dict[str, Any]:
        return {
            **self._event_key(message_id),
            "item_type": "inbound_message",
            "message_id_hash": self._digest(message_id),
            "sender_id_hash": self._digest(sender_id),
            "status": "PROCESSING",
            "lease_until": lease_until,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + _TTL_SECONDS,
        }

    def _claim_new(
        self, sender_id: str, message_id: str, now: int, lease_until: int
    ) -> None:
        event = self._new_event(
            sender_id=sender_id,
            message_id=message_id,
            now=now,
            lease_until=lease_until,
        )
        sender_key = self._sender_key(sender_id)
        self._transact(
            [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": sender_key,
                        "UpdateExpression": "SET active_message_id = :message_id, locked_at = :now",
                        "ConditionExpression": "attribute_not_exists(active_message_id)",
                        "ExpressionAttributeValues": {
                            ":message_id": self._digest(message_id),
                            ":now": now,
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": event,
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ]
        )

    def _restore_lock(self, sender_id: str, message_id: str, state: str) -> None:
        """Restore a missing lock only if the event is still in the observed state."""
        self._transact(
            [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": self._sender_key(sender_id),
                        "UpdateExpression": "SET active_message_id = :message_id, locked_at = :now",
                        "ConditionExpression": "attribute_not_exists(active_message_id)",
                        "ExpressionAttributeValues": {
                            ":message_id": self._digest(message_id),
                            ":now": int(time.time()),
                        },
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": self.table.name,
                        "Key": self._event_key(message_id),
                        "ConditionExpression": (
                            "#status = :status AND #sender_hash = :sender_hash"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#sender_hash": "sender_id_hash",
                        },
                        "ExpressionAttributeValues": {
                            ":status": state,
                            ":sender_hash": self._digest(sender_id),
                        },
                    }
                },
            ]
        )

    def claim(self, sender_id: str, message_id: str, lease_seconds: int) -> MessageClaim:
        """Claim an inbound message, returning its saved response on ordinary retries."""
        sender_id = sender_id.strip()
        message_id = message_id.strip()
        if not sender_id or not message_id:
            raise ValueError("sender_id y message_id son obligatorios.")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds debe ser positivo.")

        event_key = self._event_key(message_id)
        sender_key = self._sender_key(sender_id)
        message_hash = self._digest(message_id)

        for _attempt in range(5):
            now = int(time.time())
            event = self._get(event_key)
            if event and event.get("sender_id_hash") != self._digest(sender_id):
                raise MessageStoreConflictError(
                    "El mensaje ya está asociado a otro remitente."
                )
            if event and event.get("status") == "SENT":
                return MessageClaim("already_sent")

            sender = self._get(sender_key)
            active_message_id = (sender or {}).get("active_message_id")
            if active_message_id and active_message_id != message_hash:
                return MessageClaim("conversation_paused")

            if event is None:
                try:
                    self._claim_new(sender_id, message_id, now, now + lease_seconds)
                except self._client.exceptions.TransactionCanceledException:
                    # A concurrent duplicate may have won either conditional write.
                    continue
                return MessageClaim("claimed")

            state = str(event.get("status", ""))
            if state == "SENT":
                return MessageClaim("already_sent")

            if active_message_id is None:
                try:
                    self._restore_lock(sender_id, message_id, state)
                except self._client.exceptions.TransactionCanceledException:
                    continue
                continue

            if state == "FAILED":
                return MessageClaim("failed")
            if state == "SEND_UNKNOWN":
                return MessageClaim("send_unknown")

            if state == "REPLY_READY":
                response_text = event.get("response_text")
                if not isinstance(response_text, str):
                    raise MessageStoreConflictError("REPLY_READY no tiene respuesta persistida.")
                return MessageClaim("reply_ready", response_text)

            lease_until = int(event.get("lease_until", 0))
            if state == "PROCESSING":
                if lease_until > now:
                    return MessageClaim("in_progress")
                try:
                    self.table.update_item(
                        Key=event_key,
                        UpdateExpression=(
                            "SET lease_until = :lease, updated_at = :now, expires_at = :expires"
                        ),
                        ConditionExpression=(
                            Attr("status").eq("PROCESSING") & Attr("lease_until").lte(now)
                        ),
                        ExpressionAttributeValues={
                            ":lease": now + lease_seconds,
                            ":now": now,
                            ":expires": now + _TTL_SECONDS,
                        },
                    )
                except self._client.exceptions.ConditionalCheckFailedException:
                    continue
                return MessageClaim("claimed")

            if state == "SENDING":
                if lease_until > now:
                    return MessageClaim("in_progress")
                try:
                    self.table.update_item(
                        Key=event_key,
                        UpdateExpression=(
                            "SET #status = :unknown, updated_at = :now, failure_category = :category "
                            "REMOVE lease_until"
                        ),
                        ConditionExpression=(
                            Attr("status").eq("SENDING") & Attr("lease_until").lte(now)
                        ),
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={
                            ":unknown": "SEND_UNKNOWN",
                            ":now": now,
                            ":category": "expired_send_lease",
                        },
                    )
                except self._client.exceptions.ConditionalCheckFailedException:
                    continue
                return MessageClaim("send_unknown")

            raise MessageStoreConflictError("Estado de mensaje desconocido en DynamoDB.")

        # Re-read once after repeated conditional races, then fail closed.
        sender = self._get(sender_key)
        active_message_id = (sender or {}).get("active_message_id")
        if active_message_id and active_message_id != message_hash:
            return MessageClaim("conversation_paused")
        event = self._get(event_key)
        if event and event.get("status") == "SENT":
            return MessageClaim("already_sent")
        if event and event.get("status") == "REPLY_READY":
            response_text = event.get("response_text")
            if isinstance(response_text, str):
                return MessageClaim("reply_ready", response_text)
        return MessageClaim("in_progress")

    def save_reply(self, sender_id: str, message_id: str, response_text: str) -> None:
        now = int(time.time())
        try:
            self.table.update_item(
                Key=self._event_key(message_id),
                UpdateExpression=(
                    "SET #status = :ready, response_text = :response, updated_at = :now, "
                    "expires_at = :expires REMOVE lease_until"
                ),
                ConditionExpression=Attr("status").eq("PROCESSING"),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":ready": "REPLY_READY",
                    ":response": response_text,
                    ":now": now,
                    ":expires": now + _TTL_SECONDS,
                },
            )
        except self._client.exceptions.ConditionalCheckFailedException as error:
            event = self._get(self._event_key(message_id))
            if (
                event
                and event.get("status") == "REPLY_READY"
                and event.get("response_text") == response_text
            ):
                return
            raise MessageStoreConflictError("No se pudo persistir la respuesta del agente.") from error

    def mark_sending(self, sender_id: str, message_id: str) -> None:
        now = int(time.time())
        sender_lock = self._get(self._sender_key(sender_id))
        if (sender_lock or {}).get("active_message_id") != self._digest(message_id):
            raise MessageStoreConflictError("El mensaje no posee el turno activo del remitente.")
        try:
            self.table.update_item(
                Key=self._event_key(message_id),
                UpdateExpression=(
                    "SET #status = :sending, lease_until = :lease, updated_at = :now, expires_at = :expires"
                ),
                ConditionExpression=Attr("status").eq("REPLY_READY"),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":sending": "SENDING",
                    ":lease": now + self._send_lease_seconds,
                    ":now": now,
                    ":expires": now + _TTL_SECONDS,
                },
            )
        except self._client.exceptions.ConditionalCheckFailedException as error:
            raise MessageStoreConflictError("No se pudo iniciar el envío de WhatsApp.") from error

    def mark_sent(self, sender_id: str, message_id: str, provider_message_id: str) -> None:
        now = int(time.time())
        try:
            self._transact(
                [
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": self._event_key(message_id),
                            "UpdateExpression": (
                                "SET #status = :sent, provider_message_id = :provider_id, "
                                "sent_at = :now, updated_at = :now, expires_at = :expires "
                                "REMOVE lease_until"
                            ),
                            "ConditionExpression": "#status = :sending",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":sent": "SENT",
                                ":sending": "SENDING",
                                ":provider_id": provider_message_id,
                                ":now": now,
                                ":expires": now + _TTL_SECONDS,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": self._sender_key(sender_id),
                            "UpdateExpression": "REMOVE active_message_id, locked_at",
                            "ConditionExpression": "active_message_id = :message_id",
                            "ExpressionAttributeValues": {
                                ":message_id": self._digest(message_id),
                            },
                        }
                    },
                ]
            )
        except self._client.exceptions.TransactionCanceledException as error:
            event = self._get(self._event_key(message_id))
            if (
                event
                and event.get("status") == "SENT"
                and event.get("provider_message_id") == provider_message_id
            ):
                return
            raise MessageStoreConflictError("No se pudo confirmar el envío de WhatsApp.") from error

    def mark_send_unknown(self, sender_id: str, message_id: str) -> None:
        self._set_terminal_from_sending(
            sender_id, message_id, "SEND_UNKNOWN", "ambiguous_send"
        )

    def mark_retryable(self, sender_id: str, message_id: str) -> None:
        now = int(time.time())
        try:
            self._transact(
                [
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": self._event_key(message_id),
                            "UpdateExpression": (
                                "SET #status = :ready, updated_at = :now, expires_at = :expires "
                                "REMOVE lease_until, failure_category"
                            ),
                            "ConditionExpression": "#status = :sending",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":ready": "REPLY_READY",
                                ":sending": "SENDING",
                                ":now": now,
                                ":expires": now + _TTL_SECONDS,
                            },
                        }
                    },
                    self._active_lock_check(sender_id, message_id),
                ]
            )
        except self._client.exceptions.TransactionCanceledException as error:
            event = self._get(self._event_key(message_id))
            sender_lock = self._get(self._sender_key(sender_id))
            if (
                event
                and event.get("status") == "REPLY_READY"
                and (sender_lock or {}).get("active_message_id") == self._digest(message_id)
            ):
                return
            raise MessageStoreConflictError("No se pudo conservar la respuesta para el reintento.") from error

    def mark_failed(self, sender_id: str, message_id: str, category: str) -> None:
        self._set_failed(sender_id, message_id, category[:80] or "permanent_error")

    def _set_failed(self, sender_id: str, message_id: str, category: str) -> None:
        now = int(time.time())
        try:
            self._transact(
                [
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": self._event_key(message_id),
                            "UpdateExpression": (
                                "SET #status = :failed, failure_category = :category, "
                                "updated_at = :now, expires_at = :expires REMOVE lease_until"
                            ),
                            "ConditionExpression": "#status IN (:processing, :ready, :sending)",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":failed": "FAILED",
                                ":processing": "PROCESSING",
                                ":ready": "REPLY_READY",
                                ":sending": "SENDING",
                                ":category": category,
                                ":now": now,
                                ":expires": now + _TTL_SECONDS,
                            },
                        }
                    },
                    self._active_lock_check(sender_id, message_id),
                ]
            )
        except self._client.exceptions.TransactionCanceledException as error:
            event = self._get(self._event_key(message_id))
            if event and event.get("status") == "FAILED":
                return
            raise MessageStoreConflictError("No se pudo registrar el error permanente del mensaje.") from error

    def _active_lock_check(self, sender_id: str, message_id: str) -> dict[str, Any]:
        return {
            "ConditionCheck": {
                "TableName": self.table.name,
                "Key": self._sender_key(sender_id),
                "ConditionExpression": "active_message_id = :message_id",
                "ExpressionAttributeValues": {
                    ":message_id": self._digest(message_id),
                },
            }
        }

    def _set_terminal_from_sending(
        self, sender_id: str, message_id: str, status: str, category: str
    ) -> None:
        now = int(time.time())
        try:
            self._transact(
                [
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": self._event_key(message_id),
                            "UpdateExpression": (
                                "SET #status = :status, failure_category = :category, "
                                "updated_at = :now, expires_at = :expires REMOVE lease_until"
                            ),
                            "ConditionExpression": "#status = :sending",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":status": status,
                                ":sending": "SENDING",
                                ":category": category[:80],
                                ":now": now,
                                ":expires": now + _TTL_SECONDS,
                            },
                        }
                    },
                    self._active_lock_check(sender_id, message_id),
                ]
            )
        except self._client.exceptions.TransactionCanceledException as error:
            event = self._get(self._event_key(message_id))
            if event and event.get("status") == status:
                return
            raise MessageStoreConflictError("No se pudo registrar el resultado terminal del envío.") from error
