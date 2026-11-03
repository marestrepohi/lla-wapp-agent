"""Business tools. These are the only financial side-effect boundaries."""

from __future__ import annotations

from datetime import date

from lla_agent.domain.models import Balance, Escalation, PaymentPromise
from lla_agent.persistence.sqlite import SQLiteRepository


class BillingTool:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def consultar_saldo(self, cliente_id: str) -> Balance:
        """Return the authoritative billing balance; never infer it from conversation memory."""
        return self.repository.get_balance(cliente_id)


class PaymentsTool:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def registrar_promesa_pago(
        self, cliente_id: str, monto: int, fecha_pago: date
    ) -> PaymentPromise:
        """Persist a promise after the application has obtained explicit confirmation."""
        if monto <= 0:
            raise ValueError("El monto debe ser positivo")
        if fecha_pago < date.today():
            raise ValueError("La fecha de pago no puede estar en el pasado")
        return self.repository.save_promise(cliente_id, monto, fecha_pago)


class EscalationTool:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def escalar_a_humano(self, cliente_id: str | None, motivo: str, resumen: str) -> Escalation:
        """Create an auditable human-work queue item."""
        return self.repository.save_escalation(cliente_id, motivo, resumen)
