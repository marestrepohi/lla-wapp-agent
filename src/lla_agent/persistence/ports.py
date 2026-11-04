"""Persistence contract consumed by business tools and the LangGraph workflow."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Protocol

from lla_agent.domain.models import Balance, Escalation, PaymentPromise


class CustomerRepository(Protocol):
    """Operations required by the customer identity and collections workflow."""

    def get_client(self, client_id: str) -> Any | None: ...

    def get_client_by_external_id(self, external_id: str) -> Any | None: ...

    def link_client_to_external_id(
        self, client_id: str, external_id: str
    ) -> Literal["linked", "already_linked", "conflict", "not_found"]: ...

    def get_balance(self, client_id: str) -> Balance: ...

    def get_account_details(self, client_id: str) -> dict[str, Any] | None: ...

    def save_promise(self, client_id: str, amount_cents: int, payment_date: date) -> PaymentPromise: ...

    def save_pending_promise(self, client_id: str, amount_cents: int, payment_date: date) -> None: ...

    def get_pending_promise(self, client_id: str) -> tuple[int, date] | None: ...

    def save_escalation(self, client_id: str | None, reason: str, summary: str) -> Escalation: ...

    def record_opt_out(self, client_id: str | None, external_id: str | None) -> None: ...

    def is_opted_out(self, client_id: str | None, external_id: str | None) -> bool: ...

    def record_message(
        self,
        message_id: str,
        client_id: str | None,
        external_id: str | None,
        direction: str,
        body: str,
    ) -> None: ...
