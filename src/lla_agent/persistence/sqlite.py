"""SQLite persistence used by the local prototype.

The repository is deliberately small, but keeps the business workflow independent from SQL.
It can be replaced by DynamoDB/Aurora repositories without changing the agent or tools.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from lla_agent.domain.models import Balance, Escalation, PaymentPromise
from lla_agent.persistence.errors import ClientNotFoundError, ToolError


class SQLiteRepository:
    def __init__(self, path: str | Path | None = None, *, seed: bool = True):
        configured = path or os.getenv("LLA_DB_PATH", "data/collections.sqlite3")
        self.path = Path(configured)
        self._memory_uri: str | None = None
        self._memory_keeper: sqlite3.Connection | None = None
        if str(self.path) == ":memory:":
            self._memory_uri = f"file:lla-agent-{id(self)}?mode=memory&cache=shared"
            self._memory_keeper = sqlite3.connect(self._memory_uri, uri=True)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if seed:
            self._seed()

    def _connect(self) -> sqlite3.Connection:
        target = self._memory_uri or self.path
        connection = sqlite3.connect(target, uri=self._memory_uri is not None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    external_id TEXT UNIQUE,
                    balance_cents INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    billing_available INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS client_external_identities (
                    external_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    FOREIGN KEY(client_id) REFERENCES clients(client_id)
                );
                CREATE TABLE IF NOT EXISTS client_services (
                    service_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    service_type TEXT NOT NULL CHECK (service_type IN ('mobile', 'internet', 'tv')),
                    plan_name TEXT NOT NULL,
                    monthly_fee_cents INTEGER NOT NULL CHECK (monthly_fee_cents >= 0),
                    status TEXT NOT NULL CHECK (status IN ('active', 'suspended')),
                    FOREIGN KEY(client_id) REFERENCES clients(client_id)
                );
                CREATE INDEX IF NOT EXISTS idx_client_services_client
                    ON client_services(client_id, service_type);
                CREATE TABLE IF NOT EXISTS invoices (
                    invoice_id TEXT PRIMARY KEY,
                    service_id TEXT NOT NULL,
                    billing_period TEXT NOT NULL,
                    issue_date TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                    paid_cents INTEGER NOT NULL DEFAULT 0 CHECK (
                        paid_cents >= 0 AND paid_cents <= amount_cents
                    ),
                    status TEXT NOT NULL CHECK (status IN ('unpaid', 'partial', 'paid')),
                    UNIQUE(service_id, billing_period),
                    CHECK (
                        (status = 'unpaid' AND paid_cents = 0)
                        OR (status = 'partial' AND paid_cents > 0 AND paid_cents < amount_cents)
                        OR (status = 'paid' AND paid_cents = amount_cents)
                    ),
                    FOREIGN KEY(service_id) REFERENCES client_services(service_id)
                );
                CREATE INDEX IF NOT EXISTS idx_invoices_service_due
                    ON invoices(service_id, due_date);
                CREATE TABLE IF NOT EXISTS collection_events (
                    event_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    FOREIGN KEY(client_id) REFERENCES clients(client_id)
                );
                CREATE INDEX IF NOT EXISTS idx_collection_events_client_time
                    ON collection_events(client_id, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS promises (
                    promise_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    payment_date TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(client_id) REFERENCES clients(client_id)
                );
                CREATE TABLE IF NOT EXISTS escalations (
                    escalation_id TEXT PRIMARY KEY,
                    client_id TEXT,
                    reason TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS opt_outs (
                    client_id TEXT,
                    external_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(client_id, external_id)
                );
                CREATE TABLE IF NOT EXISTS pending_promises (
                    client_id TEXT PRIMARY KEY,
                    amount_cents INTEGER NOT NULL,
                    payment_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    client_id TEXT,
                    external_id TEXT,
                    direction TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _seed(self) -> None:
        clients = [
            ("C001", "Ana Torres", "telegram:demo-001", 125000, "PAB", "2026-11-15", "overdue", 1),
            ("C002", "Luis Pérez", "telegram:demo-002", 45000, "PAB", "2026-11-10", "overdue", 1),
            ("C003", "María Gómez", "telegram:demo-003", 83000, "PAB", "2026-11-05", "overdue", 1),
            ("CERR", "Cliente Prueba", "telegram:demo-error", 0, "PAB", "2026-11-01", "overdue", 0),
            ("1234", "Cliente QA 1234", None, 12500, "PAB", "2026-11-15", "overdue", 1),
            ("5678", "Cliente QA 5678", None, 45000, "PAB", "2026-11-20", "overdue", 1),
            ("9012", "Cliente QA 9012", None, 8300, "PAB", "2026-11-25", "overdue", 1),
        ]
        with self._connect() as db:
            db.executemany(
                """
                INSERT OR IGNORE INTO clients
                (client_id, full_name, external_id, balance_cents, currency, due_date, status, billing_available)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                clients,
            )
            services = [
                ("SVC-QA1234-MOB", "1234", "mobile", "Móvil Conecta 35 GB", 3900, "active"),
                ("SVC-QA1234-NET", "1234", "internet", "Fibra Hogar 600 Mbps", 5900, "active"),
                ("SVC-QA5678-NET", "5678", "internet", "Fibra Hogar 750 Mbps", 7500, "active"),
                ("SVC-QA5678-TV", "5678", "tv", "TV Familiar HD", 2800, "active"),
                ("SVC-QA9012-MOB", "9012", "mobile", "Móvil Control 20 GB", 3500, "active"),
                ("SVC-QA9012-NET", "9012", "internet", "Internet Hogar 400 Mbps", 4800, "active"),
            ]
            db.executemany(
                """
                INSERT OR IGNORE INTO client_services
                (service_id, client_id, service_type, plan_name, monthly_fee_cents, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                services,
            )

            invoices = [
                ("INV-QA1234-MOB-202608", "SVC-QA1234-MOB", "2026-08", "2026-08-01", "2026-09-10", 3900, 0, "unpaid"),
                ("INV-QA1234-MOB-202609", "SVC-QA1234-MOB", "2026-09", "2026-09-01", "2026-10-10", 3900, 0, "unpaid"),
                ("INV-QA1234-NET-202608", "SVC-QA1234-NET", "2026-08", "2026-08-01", "2026-09-12", 5900, 0, "unpaid"),
                ("INV-QA1234-NET-202609", "SVC-QA1234-NET", "2026-09", "2026-09-01", "2026-10-12", 5900, 0, "unpaid"),
                ("INV-QA5678-NET-202608", "SVC-QA5678-NET", "2026-08", "2026-08-01", "2026-09-08", 7500, 0, "unpaid"),
                ("INV-QA5678-NET-202609", "SVC-QA5678-NET", "2026-09", "2026-09-01", "2026-10-08", 7500, 0, "unpaid"),
                ("INV-QA5678-TV-202609", "SVC-QA5678-TV", "2026-09", "2026-09-01", "2026-10-12", 2800, 0, "unpaid"),
                ("INV-QA9012-MOB-202609", "SVC-QA9012-MOB", "2026-09", "2026-09-01", "2026-10-08", 3500, 0, "unpaid"),
                ("INV-QA9012-NET-202608", "SVC-QA9012-NET", "2026-08", "2026-08-01", "2026-09-10", 4800, 4800, "paid"),
                ("INV-QA9012-NET-202609", "SVC-QA9012-NET", "2026-09", "2026-09-01", "2026-10-10", 4800, 2000, "partial"),
            ]
            db.executemany(
                """
                INSERT OR IGNORE INTO invoices
                (invoice_id, service_id, billing_period, issue_date, due_date,
                 amount_cents, paid_cents, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                invoices,
            )

            events = [
                ("EV-QA1234-01", "1234", "2026-10-14T15:30:00+00:00", "reminder", "Se envió un recordatorio por las facturas vencidas de telefonía e internet.", "sin respuesta"),
                ("EV-QA1234-02", "1234", "2026-10-20T16:15:00+00:00", "customer_contact", "La persona pidió recibir recordatorios después de las 17:00.", "preferencia registrada en este expediente de prueba"),
                ("EV-QA1234-03", "1234", "2026-10-25T14:00:00+00:00", "call_attempt", "Se intentó contactar para revisar el saldo vencido.", "no se logró establecer contacto"),
                ("EV-QA5678-01", "5678", "2026-10-12T15:00:00+00:00", "payment_promise", "Se acordó un pago de PAB 100.00 para el 20/10/2026.", "promesa incumplida; no aparece pago aplicado"),
                ("EV-QA5678-02", "5678", "2026-10-24T17:30:00+00:00", "customer_contact", "La persona indicó que no pudo completar el pago acordado y pidió revisar el detalle de internet y TV.", "pendiente de seguimiento"),
                ("EV-QA5678-03", "5678", "2026-11-01T14:00:00+00:00", "reminder", "Se envió un recordatorio por el saldo vencido de los servicios del hogar.", "entregado; sin respuesta registrada"),
                ("EV-QA9012-01", "9012", "2026-10-20T16:00:00+00:00", "reminder", "Se notificó el saldo pendiente de telefonía e internet.", "entregado"),
                ("EV-QA9012-02", "9012", "2026-10-28T13:45:00+00:00", "payment", "Se recibió un pago parcial de PAB 20.00 y se aplicó a la factura de internet de septiembre.", "saldo restante de esa factura: PAB 28.00"),
                ("EV-QA9012-03", "9012", "2026-10-29T15:15:00+00:00", "customer_contact", "La persona pidió confirmar cuánto queda pendiente después del pago parcial.", "requiere respuesta con saldo actualizado"),
            ]
            db.executemany(
                """
                INSERT OR IGNORE INTO collection_events
                (event_id, client_id, occurred_at, event_type, summary, outcome)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                events,
            )

            db.execute(
                """
                UPDATE clients
                SET balance_cents = COALESCE((
                        SELECT SUM(invoices.amount_cents - invoices.paid_cents)
                        FROM client_services
                        JOIN invoices USING (service_id)
                        WHERE client_services.client_id = clients.client_id
                          AND invoices.status IN ('unpaid', 'partial')
                          AND invoices.amount_cents > invoices.paid_cents
                    ), balance_cents),
                    due_date = COALESCE((
                        SELECT MIN(invoices.due_date)
                        FROM client_services
                        JOIN invoices USING (service_id)
                        WHERE client_services.client_id = clients.client_id
                          AND invoices.status IN ('unpaid', 'partial')
                          AND invoices.amount_cents > invoices.paid_cents
                    ), due_date),
                    status = CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM client_services
                            JOIN invoices USING (service_id)
                            WHERE client_services.client_id = clients.client_id
                              AND invoices.status IN ('unpaid', 'partial')
                              AND invoices.amount_cents > invoices.paid_cents
                              AND invoices.due_date < date('now')
                        ) THEN 'overdue'
                        ELSE status
                    END
                WHERE client_id IN ('1234', '5678', '9012')
                """
            )

    def get_client(self, client_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()

    def get_client_by_external_id(self, external_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            linked = db.execute(
                """
                SELECT clients.* FROM client_external_identities
                JOIN clients USING (client_id)
                WHERE client_external_identities.external_id = ?
                """,
                (external_id,),
            ).fetchone()
            if linked is not None:
                return linked
            return db.execute("SELECT * FROM clients WHERE external_id = ?", (external_id,)).fetchone()

    def link_client_to_external_id(
        self, client_id: str, external_id: str
    ) -> Literal["linked", "already_linked", "conflict", "not_found"]:
        """Create a one-way QA identity link without allowing a sender to switch profiles."""
        normalized_client_id = client_id.strip().upper()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM clients WHERE client_id = ?", (normalized_client_id,)
            ).fetchone() is None:
                return "not_found"

            existing = db.execute(
                "SELECT client_id FROM client_external_identities WHERE external_id = ?",
                (external_id,),
            ).fetchone()
            if existing is None:
                existing = db.execute(
                    "SELECT client_id FROM clients WHERE external_id = ?", (external_id,)
                ).fetchone()
            if existing is not None:
                return "already_linked" if existing["client_id"] == normalized_client_id else "conflict"

            db.execute(
                """
                INSERT INTO client_external_identities (external_id, client_id, linked_at)
                VALUES (?, ?, ?)
                """,
                (external_id, normalized_client_id, datetime.now(UTC).isoformat()),
            )
        return "linked"

    def get_balance(self, client_id: str) -> Balance:
        row = self.get_client(client_id)
        if row is None:
            raise ClientNotFoundError(f"Cliente desconocido: {client_id}")
        if not row["billing_available"]:
            raise ToolError("El sistema de facturación no está disponible")

        with self._connect() as db:
            invoices = db.execute(
                """
                SELECT
                    COUNT(invoices.invoice_id) AS invoice_count,
                    SUM(CASE
                        WHEN invoices.status IN ('unpaid', 'partial')
                             AND invoices.amount_cents > invoices.paid_cents
                        THEN invoices.amount_cents - invoices.paid_cents
                        ELSE 0
                    END) AS open_balance_cents,
                    MIN(CASE
                        WHEN invoices.status IN ('unpaid', 'partial')
                             AND invoices.amount_cents > invoices.paid_cents
                        THEN invoices.due_date
                    END) AS oldest_open_due_date
                FROM client_services
                LEFT JOIN invoices USING (service_id)
                WHERE client_services.client_id = ?
                """,
                (client_id,),
            ).fetchone()

        if invoices["invoice_count"]:
            amount_cents = int(invoices["open_balance_cents"] or 0)
            due_value = invoices["oldest_open_due_date"]
            due_date = date.fromisoformat(due_value) if due_value else None
            if due_date is None:
                status = "paid"
            else:
                status = "overdue" if due_date < date.today() else "current"
            return Balance(
                cliente_id=row["client_id"],
                amount_cents=amount_cents,
                currency=row["currency"],
                due_date=due_date,
                status=status,
            )

        return Balance(
            cliente_id=row["client_id"],
            amount_cents=row["balance_cents"],
            currency=row["currency"],
            due_date=date.fromisoformat(row["due_date"]),
            status=row["status"],
        )

    def get_account_details(self, client_id: str) -> dict[str, Any] | None:
        """Return the linked client's plans, invoices, and synthetic collection history."""
        client = self.get_client(client_id)
        if client is None:
            return None
        balance = self.get_balance(client_id)
        with self._connect() as db:
            services = db.execute(
                """
                SELECT service_id, service_type, plan_name, monthly_fee_cents, status
                FROM client_services
                WHERE client_id = ?
                ORDER BY service_type, service_id
                """,
                (client_id,),
            ).fetchall()
            invoices = db.execute(
                """
                SELECT invoices.invoice_id, invoices.service_id, client_services.service_type,
                       client_services.plan_name, invoices.billing_period, invoices.issue_date,
                       invoices.due_date, invoices.amount_cents, invoices.paid_cents,
                       invoices.status AS payment_status
                FROM invoices
                JOIN client_services USING (service_id)
                WHERE client_services.client_id = ?
                ORDER BY invoices.due_date, invoices.invoice_id
                """,
                (client_id,),
            ).fetchall()
            events = db.execute(
                """
                SELECT event_id, occurred_at, event_type, summary, outcome
                FROM collection_events
                WHERE client_id = ?
                ORDER BY occurred_at DESC, event_id
                LIMIT 10
                """,
                (client_id,),
            ).fetchall()

        invoice_details = []
        for invoice in invoices:
            outstanding = int(invoice["amount_cents"] - invoice["paid_cents"])
            due_date = date.fromisoformat(invoice["due_date"])
            if outstanding <= 0 or invoice["payment_status"] == "paid":
                status = "paid"
            else:
                status = "overdue" if due_date < date.today() else "open"
            invoice_details.append(
                {
                    "invoice_id": invoice["invoice_id"],
                    "service_id": invoice["service_id"],
                    "service_type": invoice["service_type"],
                    "plan_name": invoice["plan_name"],
                    "billing_period": invoice["billing_period"],
                    "issue_date": invoice["issue_date"],
                    "due_date": invoice["due_date"],
                    "amount_cents": int(invoice["amount_cents"]),
                    "paid_cents": int(invoice["paid_cents"]),
                    "outstanding_cents": max(outstanding, 0),
                    "status": status,
                }
            )

        return {
            "client_id": client["client_id"],
            "full_name": client["full_name"],
            "currency": balance.currency,
            "balance_cents": balance.amount_cents,
            "balance_status": balance.status,
            "oldest_open_due_date": balance.due_date.isoformat() if balance.due_date else None,
            "services": [dict(service) for service in services],
            "invoices": invoice_details,
            "collection_events": [dict(event) for event in events],
        }

    def save_promise(self, client_id: str, amount_cents: int, payment_date: date) -> PaymentPromise:
        if self.get_client(client_id) is None:
            raise ClientNotFoundError(f"Cliente desconocido: {client_id}")
        now = datetime.now(UTC)
        promise_id = f"PROM-{uuid.uuid4().hex[:10].upper()}"
        key = f"{client_id}:{amount_cents}:{payment_date.isoformat()}"
        with self._connect() as db:
            existing = db.execute(
                "SELECT * FROM promises WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing:
                promise_id = existing["promise_id"]
                confirmed_at = datetime.fromisoformat(existing["confirmed_at"])
            else:
                db.execute(
                    """
                    INSERT INTO promises
                    (promise_id, client_id, amount_cents, payment_date, confirmed_at, idempotency_key)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        promise_id,
                        client_id,
                        amount_cents,
                        payment_date.isoformat(),
                        now.isoformat(),
                        key,
                    ),
                )
                confirmed_at = now
            db.execute("DELETE FROM pending_promises WHERE client_id = ?", (client_id,))
        return PaymentPromise(promise_id, client_id, amount_cents, payment_date, confirmed_at)

    def save_pending_promise(self, client_id: str, amount_cents: int, payment_date: date) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO pending_promises (client_id, amount_cents, payment_date, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    amount_cents=excluded.amount_cents,
                    payment_date=excluded.payment_date,
                    created_at=excluded.created_at
                """,
                (client_id, amount_cents, payment_date.isoformat(), datetime.now(UTC).isoformat()),
            )

    def get_pending_promise(self, client_id: str) -> tuple[int, date] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT amount_cents, payment_date FROM pending_promises WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        return (row["amount_cents"], date.fromisoformat(row["payment_date"])) if row else None

    def save_escalation(self, client_id: str | None, reason: str, summary: str) -> Escalation:
        escalation = Escalation(
            f"ESC-{uuid.uuid4().hex[:10].upper()}",
            client_id,
            reason,
            summary,
            datetime.now(UTC),
        )
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO escalations (escalation_id, client_id, reason, summary, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    escalation.escalation_id,
                    client_id,
                    reason,
                    summary,
                    escalation.created_at.isoformat(),
                ),
            )
        return escalation

    def record_opt_out(self, client_id: str | None, external_id: str | None) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO opt_outs (client_id, external_id, created_at) VALUES (?, ?, ?)",
                (client_id, external_id, datetime.now(UTC).isoformat()),
            )

    def is_opted_out(self, client_id: str | None, external_id: str | None) -> bool:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT 1 FROM opt_outs
                WHERE (client_id IS NOT NULL AND client_id = ?)
                   OR (external_id IS NOT NULL AND external_id = ?)
                LIMIT 1
                """,
                (client_id, external_id),
            ).fetchone()
        return row is not None

    def record_message(
        self,
        message_id: str,
        client_id: str | None,
        external_id: str | None,
        direction: str,
        body: str,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO messages
                (message_id, client_id, external_id, direction, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    client_id,
                    external_id,
                    direction,
                    body,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def count_rows(self, table: str) -> int:
        if table not in {"promises", "escalations", "opt_outs"}:
            raise ValueError("unsupported table")
        with self._connect() as db:
            return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def all_promises(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM promises ORDER BY confirmed_at")]
