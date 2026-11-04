"""DynamoDB-backed repository for customer identity and collections records."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, date, datetime
from functools import wraps
from typing import Any, Callable, Literal, TypeVar, cast

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from lla_agent.domain.models import Balance, Escalation, PaymentPromise
from lla_agent.persistence.errors import ClientNotFoundError, ToolError

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


def _translate_dynamo_errors(function: Callable[..., _T]) -> Callable[..., _T]:
    """Convert AWS transport/service errors into the repository error contract."""

    @wraps(function)
    def wrapped(self: DynamoDBRepository, *args: Any, **kwargs: Any) -> _T:
        try:
            return function(self, *args, **kwargs)
        except (BotoCoreError, ClientError) as error:
            _LOGGER.warning("DynamoDB operation failed (%s)", type(error).__name__)
            raise ToolError("No fue posible consultar el repositorio de clientes.") from error

    return cast(Callable[..., _T], wrapped)


class DynamoDBRepository:
    """Implement the business repository contract over one composite-key table.

    Customer records use ``pk=CLIENT#<id>`` and sort keys such as ``PROFILE``,
    ``SERVICE#<id>``, ``INVOICE#<id>``, and ``EVENT#<timestamp>#<id>``. External
    identities are stored under a SHA-256-derived partition key.
    """

    def __init__(
        self,
        table_name: str,
        *,
        conversation_table_name: str | None = None,
        dynamodb: Any | None = None,
        config: Config | None = None,
    ) -> None:
        if not table_name.strip():
            raise ValueError("DYNAMODB_CUSTOMER_TABLE debe tener un nombre.")
        if conversation_table_name is not None and not conversation_table_name.strip():
            raise ValueError("DYNAMODB_CONVERSATION_TABLE no puede estar vacío.")
        resource = dynamodb or boto3.resource("dynamodb", config=config)
        self.table = resource.Table(table_name)
        # Keep the one-table form for local tooling and older seed scripts. AWS runtime
        # explicitly supplies a dedicated table so conversation bodies never share billing data.
        self.conversation_table = resource.Table(conversation_table_name or table_name)

    @staticmethod
    def _client_pk(client_id: str) -> str:
        return f"CLIENT#{client_id.strip().upper()}"

    @staticmethod
    def _external_hash(external_id: str) -> str:
        digest = hashlib.sha256(external_id.strip().encode("utf-8")).hexdigest()
        return f"EXTERNAL#{digest}"

    @staticmethod
    def _conversation_pk(external_id: str | None, client_id: str | None) -> str:
        source = external_id or client_id or "anonymous"
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return f"CONVERSATION#{digest}"

    def _get_item(self, pk: str, sk: str) -> dict[str, Any] | None:
        return self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")

    def _query_partition(self, pk: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        query: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(pk),
            "ConsistentRead": True,
        }
        while True:
            page = self.table.query(**query)
            items.extend(page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                break
            query["ExclusiveStartKey"] = last_key
        return items

    def _put_once(self, item: dict[str, Any]) -> bool:
        try:
            self.table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    @_translate_dynamo_errors
    def seed_demo_data(self) -> int:
        """Insert synthetic QA records without overwriting records already in the table."""
        from lla_agent.persistence.dynamodb_seed import demo_case_items

        created = 0
        for item in demo_case_items():
            if self._put_once(item):
                created += 1
        return created

    @_translate_dynamo_errors
    def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self._get_item(self._client_pk(client_id), "PROFILE")

    @_translate_dynamo_errors
    def get_client_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        identity = self._get_item(self._external_hash(external_id), "IDENTITY")
        if identity is None:
            return None
        return self.get_client(str(identity["client_id"]))

    @_translate_dynamo_errors
    def link_client_to_external_id(
        self, client_id: str, external_id: str
    ) -> Literal["linked", "already_linked", "conflict", "not_found"]:
        """Link a sender once; concurrent attempts cannot replace an existing profile."""
        normalized_client_id = client_id.strip().upper()
        if self.get_client(normalized_client_id) is None:
            return "not_found"

        identity_pk = self._external_hash(external_id)
        existing = self._get_item(identity_pk, "IDENTITY")
        if existing is not None:
            return (
                "already_linked"
                if str(existing.get("client_id")) == normalized_client_id
                else "conflict"
            )

        item = {
            "pk": identity_pk,
            "sk": "IDENTITY",
            "item_type": "identity",
            "client_id": normalized_client_id,
            "linked_at": datetime.now(UTC).isoformat(),
        }
        if self._put_once(item):
            return "linked"

        existing = self._get_item(identity_pk, "IDENTITY")
        if existing is None:
            raise ToolError("No fue posible completar la vinculación del cliente.")
        return (
            "already_linked"
            if str(existing.get("client_id")) == normalized_client_id
            else "conflict"
        )

    @staticmethod
    def _balance_from_items(client_id: str, items: list[dict[str, Any]]) -> Balance:
        profile = next((item for item in items if item.get("item_type") == "profile"), None)
        if profile is None:
            raise ClientNotFoundError(f"Cliente desconocido: {client_id}")
        if not profile.get("billing_available", True):
            raise ToolError("El sistema de facturación no está disponible.")

        service_ids = {
            str(item["service_id"])
            for item in items
            if item.get("item_type") == "service"
        }
        invoices = [
            item
            for item in items
            if item.get("item_type") == "invoice"
            and str(item.get("service_id")) in service_ids
        ]
        if invoices:
            open_invoices = [
                invoice
                for invoice in invoices
                if invoice.get("status") in {"unpaid", "partial"}
                and int(invoice.get("amount_cents", 0)) > int(invoice.get("paid_cents", 0))
            ]
            amount_cents = sum(
                int(invoice["amount_cents"]) - int(invoice.get("paid_cents", 0))
                for invoice in open_invoices
            )
            due_dates = [
                date.fromisoformat(str(invoice["due_date"]))
                for invoice in open_invoices
                if invoice.get("due_date")
            ]
            oldest_due_date = min(due_dates) if due_dates else None
            status = (
                "paid"
                if oldest_due_date is None
                else "overdue"
                if oldest_due_date < date.today()
                else "current"
            )
            return Balance(
                cliente_id=str(profile["client_id"]),
                amount_cents=amount_cents,
                currency=str(profile.get("currency", "PAB")),
                due_date=oldest_due_date,
                status=status,
            )

        due_value = profile.get("due_date")
        return Balance(
            cliente_id=str(profile["client_id"]),
            amount_cents=int(profile.get("balance_cents", 0)),
            currency=str(profile.get("currency", "PAB")),
            due_date=date.fromisoformat(str(due_value)) if due_value else None,
            status=str(profile.get("status", "current")),
        )

    @_translate_dynamo_errors
    def get_balance(self, client_id: str) -> Balance:
        return self._balance_from_items(client_id, self._query_partition(self._client_pk(client_id)))

    @_translate_dynamo_errors
    def get_account_details(self, client_id: str) -> dict[str, Any] | None:
        items = self._query_partition(self._client_pk(client_id))
        profile = next((item for item in items if item.get("item_type") == "profile"), None)
        if profile is None:
            return None

        balance = self._balance_from_items(client_id, items)
        services = sorted(
            (item for item in items if item.get("item_type") == "service"),
            key=lambda item: (str(item.get("service_type", "")), str(item.get("service_id", ""))),
        )
        service_by_id = {str(item["service_id"]): item for item in services}
        invoices = []
        for invoice in sorted(
            (item for item in items if item.get("item_type") == "invoice"),
            key=lambda item: (str(item.get("due_date", "")), str(item.get("invoice_id", ""))),
        ):
            service_id = str(invoice.get("service_id", ""))
            service = service_by_id.get(service_id)
            if service is None:
                continue
            amount_cents = int(invoice.get("amount_cents", 0))
            paid_cents = int(invoice.get("paid_cents", 0))
            outstanding_cents = max(amount_cents - paid_cents, 0)
            due_date = date.fromisoformat(str(invoice["due_date"]))
            payment_status = str(invoice.get("status", "unpaid"))
            status = (
                "paid"
                if outstanding_cents == 0 or payment_status == "paid"
                else "overdue"
                if due_date < date.today()
                else "open"
            )
            invoices.append(
                {
                    "invoice_id": str(invoice["invoice_id"]),
                    "service_id": service_id,
                    "service_type": str(service.get("service_type", "")),
                    "plan_name": str(service.get("plan_name", "")),
                    "billing_period": str(invoice.get("billing_period", "")),
                    "issue_date": str(invoice.get("issue_date", "")),
                    "due_date": due_date.isoformat(),
                    "amount_cents": amount_cents,
                    "paid_cents": paid_cents,
                    "outstanding_cents": outstanding_cents,
                    "status": status,
                }
            )

        events = sorted(
            (item for item in items if item.get("item_type") == "event"),
            key=lambda item: (str(item.get("occurred_at", "")), str(item.get("event_id", ""))),
            reverse=True,
        )[:10]
        return {
            "client_id": str(profile["client_id"]),
            "full_name": str(profile.get("full_name", "")),
            "currency": balance.currency,
            "balance_cents": balance.amount_cents,
            "balance_status": balance.status,
            "oldest_open_due_date": balance.due_date.isoformat() if balance.due_date else None,
            "services": [
                {
                    "service_id": str(service["service_id"]),
                    "service_type": str(service.get("service_type", "")),
                    "plan_name": str(service.get("plan_name", "")),
                    "monthly_fee_cents": int(service.get("monthly_fee_cents", 0)),
                    "status": str(service.get("status", "active")),
                }
                for service in services
            ],
            "invoices": invoices,
            "collection_events": [
                {
                    "event_id": str(event["event_id"]),
                    "occurred_at": str(event.get("occurred_at", "")),
                    "event_type": str(event.get("event_type", "")),
                    "summary": str(event.get("summary", "")),
                    "outcome": str(event.get("outcome", "")),
                }
                for event in events
            ],
        }

    @_translate_dynamo_errors
    def save_promise(self, client_id: str, amount_cents: int, payment_date: date) -> PaymentPromise:
        if self.get_client(client_id) is None:
            raise ClientNotFoundError(f"Cliente desconocido: {client_id}")
        now = datetime.now(UTC)
        idempotency_key = f"{client_id.strip().upper()}:{amount_cents}:{payment_date.isoformat()}"
        promise_id = f"PROM-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:10].upper()}"
        item = {
            "pk": self._client_pk(client_id),
            "sk": f"PROMISE#{hashlib.sha256(idempotency_key.encode()).hexdigest()}",
            "item_type": "promise",
            "promise_id": promise_id,
            "client_id": client_id.strip().upper(),
            "amount_cents": amount_cents,
            "payment_date": payment_date.isoformat(),
            "confirmed_at": now.isoformat(),
            "idempotency_key": idempotency_key,
        }
        created = self._put_once(item)
        stored = item if created else self._get_item(item["pk"], item["sk"])
        if stored is None:
            raise ToolError("No fue posible guardar la promesa de pago.")
        self.table.delete_item(Key={"pk": item["pk"], "sk": "PENDING_PROMISE"})
        return PaymentPromise(
            promise_id=str(stored["promise_id"]),
            cliente_id=str(stored["client_id"]),
            amount_cents=int(stored["amount_cents"]),
            payment_date=date.fromisoformat(str(stored["payment_date"])),
            confirmed_at=datetime.fromisoformat(str(stored["confirmed_at"])),
        )

    @_translate_dynamo_errors
    def save_pending_promise(self, client_id: str, amount_cents: int, payment_date: date) -> None:
        self.table.put_item(
            Item={
                "pk": self._client_pk(client_id),
                "sk": "PENDING_PROMISE",
                "item_type": "pending_promise",
                "client_id": client_id.strip().upper(),
                "amount_cents": amount_cents,
                "payment_date": payment_date.isoformat(),
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    @_translate_dynamo_errors
    def get_pending_promise(self, client_id: str) -> tuple[int, date] | None:
        item = self._get_item(self._client_pk(client_id), "PENDING_PROMISE")
        if item is None:
            return None
        return int(item["amount_cents"]), date.fromisoformat(str(item["payment_date"]))

    @_translate_dynamo_errors
    def save_escalation(self, client_id: str | None, reason: str, summary: str) -> Escalation:
        now = datetime.now(UTC)
        escalation = Escalation(
            f"ESC-{uuid.uuid4().hex[:10].upper()}", client_id, reason, summary, now
        )
        pk = self._client_pk(client_id) if client_id else "SYSTEM#ESCALATIONS"
        self.table.put_item(
            Item={
                "pk": pk,
                "sk": f"ESCALATION#{now.isoformat()}#{escalation.escalation_id}",
                "item_type": "escalation",
                "escalation_id": escalation.escalation_id,
                "client_id": client_id,
                "reason": reason,
                "summary": summary,
                "created_at": now.isoformat(),
            }
        )
        return escalation

    @_translate_dynamo_errors
    def record_opt_out(self, client_id: str | None, external_id: str | None) -> None:
        now = datetime.now(UTC).isoformat()
        keys: list[tuple[str, str]] = []
        if client_id:
            external_digest = hashlib.sha256((external_id or "").encode()).hexdigest()
            keys.append((self._client_pk(client_id), f"OPT_OUT#{external_digest}"))
        if external_id:
            keys.append((self._external_hash(external_id), "OPT_OUT"))
        for pk, sk in keys:
            self._put_once(
                {
                    "pk": pk,
                    "sk": sk,
                    "item_type": "opt_out",
                    "client_id": client_id,
                    "created_at": now,
                }
            )

    @_translate_dynamo_errors
    def is_opted_out(self, client_id: str | None, external_id: str | None) -> bool:
        if external_id and self._get_item(self._external_hash(external_id), "OPT_OUT") is not None:
            return True
        if client_id:
            items = self._query_partition(self._client_pk(client_id))
            return any(item.get("item_type") == "opt_out" for item in items)
        return False

    @_translate_dynamo_errors
    def record_message(
        self,
        message_id: str,
        client_id: str | None,
        external_id: str | None,
        direction: str,
        body: str,
    ) -> None:
        digest = hashlib.sha256(f"{direction}\0{message_id}".encode()).hexdigest()
        created_at = datetime.now(UTC)
        item = {
            "pk": self._conversation_pk(external_id, client_id),
            "sk": f"MESSAGE#{digest}",
            "item_type": "message",
            "message_id": message_id,
            "client_id": client_id,
            "external_id_hash": self._external_hash(external_id).removeprefix("EXTERNAL#")
            if external_id
            else None,
            "direction": direction,
            "body": body,
            "created_at": created_at.isoformat(),
            "expires_at": int(created_at.timestamp()) + 90 * 24 * 60 * 60,
        }
        try:
            self.conversation_table.put_item(
                Item=item,
                ConditionExpression=Attr("pk").not_exists(),
            )
        except self.conversation_table.meta.client.exceptions.ConditionalCheckFailedException:
            # Message audit keys are deterministic, so duplicate webhook delivery is harmless.
            pass
