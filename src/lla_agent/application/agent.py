"""Channel-independent collections policy shared by the demo and LangGraph."""

from __future__ import annotations

import re
import sqlite3
import uuid
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from lla_agent.domain.models import AgentResponse, Balance, Intent, ToolCall
from lla_agent.persistence.sqlite import ClientNotFoundError, SQLiteRepository, ToolError
from lla_agent.tools.business import BillingTool, EscalationTool, PaymentsTool

OPT_OUT_TERMS = ("stop", "baja", "cancelar", "no me contacten", "no quiero mensajes", "salir")
CONFIRMATION_COMMAND = re.compile(
    r"^confirmo propuesta (?P<amount>\d+\.\d{2}) (?P<payment_date>20\d{2}-\d{2}-\d{2})$"
)
DISPUTE_TERMS = ("no reconozco", "disputa", "disputo", "incorrecto", "no es mío", "no corresponde")
DISCOUNT_TERMS = ("descuento", "rebaja", "quita", "condonar", "condonación")
HUMAN_TERMS = ("humano", "asesor", "agente", "llamada", "supervisor")
ACCOUNT_DETAIL_TERMS = (
    "factura",
    "plan",
    "servicio",
    "promesa",
    "pago parcial",
    "pago aplicado",
    "gestión",
    "historial",
)
ACCOUNT_DETAIL_QUESTION_TERMS = (
    "qué",
    "que",
    "cuál",
    "cual",
    "cuándo",
    "cuando",
    "cuánto",
    "cuanto",
    "muéstrame",
    "muestrame",
    "detalle",
    "detállame",
    "detallame",
    "historial",
    "anterior",
    "pasó",
    "paso",
    "ver",
    "revisar",
    "consulta",
    "consultar",
    "conocer",
    "explica",
    "explícame",
    "explicame",
)
WEEKDAYS = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "domingo": 6,
}


class CollectionsAgent:
    """Deterministic collection operations called by the LangGraph workflow."""

    def __init__(self, repository: SQLiteRepository | None = None):
        self.repository = repository or SQLiteRepository()
        self.billing = BillingTool(self.repository)
        self.payments = PaymentsTool(self.repository)
        self.escalations = EscalationTool(self.repository)

    def begin_turn(
        self,
        text: str,
        *,
        client_id: str | None,
        external_id: str | None,
        channel: str,
        message_id: str,
    ) -> str | None:
        """Record an inbound message and resolve identity from trusted channel context."""
        resolved_id = self._resolve_client(client_id, external_id)
        normalized = text.strip()
        self.repository.record_message(message_id, resolved_id, external_id, "inbound", normalized)
        return resolved_id

    def opt_out_response(
        self, text: str, client_id: str | None, external_id: str | None
    ) -> AgentResponse | None:
        """Apply channel suppression before any model, RAG, or billing operation."""
        normalized = text.strip()
        if self._contains(normalized, OPT_OUT_TERMS):
            self.repository.record_opt_out(client_id, external_id)
            return AgentResponse(
                "Listo, registré tu solicitud de no recibir mensajes de cobranza y la respetaré.",
                Intent.OPT_OUT,
                opted_out=True,
                identity_validated=client_id is not None,
            )
        if self.repository.is_opted_out(client_id, external_id):
            return AgentResponse(
                "Tu solicitud de no recibir mensajes sigue activa. No enviaré más mensajes de cobranza.",
                Intent.OPT_OUT,
                opted_out=True,
                identity_validated=client_id is not None,
            )
        return None

    def validate_identity(
        self, client_id: str | None, external_id: str | None = None
    ) -> AgentResponse | None:
        """Return a safe response when a collection route lacks a valid client identity."""
        if client_id is None:
            return AgentResponse(
                "Esta conversación aún no está asociada a un perfil de cliente, así que no puedo consultar "
                "datos de cuenta. Si quieres revisar una cuenta, puedo ayudarte con información general "
                "mientras se valida el perfil.",
                Intent.UNKNOWN,
            )
        try:
            client = self.repository.get_client(client_id)
        except sqlite3.Error:
            client = None
        if client is None:
            return AgentResponse(
                "No pude vincular esta conversación a un perfil disponible, así que no consultaré datos "
                "de cuenta. Pide a quien administra el acceso que revise la vinculación.",
                Intent.UNKNOWN,
                identity_validated=False,
            )
        return None

    def classify_intent(self, text: str, client_id: str | None) -> Intent:
        """Use the shared collection intent rules; unknown/general questions go to knowledge RAG."""
        return self._classify(text, client_id)

    def lookup_balance(self, client_id: str) -> tuple[Balance | None, list[ToolCall]]:
        """Read the authoritative balance and capture the tool outcome for the graph trace."""
        try:
            balance = self.billing.consultar_saldo(client_id)
        except (ClientNotFoundError, ToolError, sqlite3.Error) as error:
            return None, [ToolCall("consultar_saldo", {"cliente_id": client_id}, f"error: {error}")]
        return balance, [ToolCall("consultar_saldo", {"cliente_id": client_id}, "success")]

    def balance_response(
        self, balance: Balance, intent: Intent, tool_calls: list[ToolCall]
    ) -> AgentResponse:
        if balance.amount_cents <= 0:
            return AgentResponse(
                "No hay saldo pendiente registrado para esta cuenta.",
                intent,
                tool_calls=tool_calls,
                identity_validated=True,
            )

        response = f"Tu saldo pendiente es {balance.amount_display}."
        if balance.due_date is not None:
            if balance.status == "overdue":
                response += f" La factura pendiente más antigua venció el {balance.due_date:%d/%m/%Y}."
            else:
                response += f" La próxima factura pendiente vence el {balance.due_date:%d/%m/%Y}."

        try:
            account = self.repository.get_account_details(balance.cliente_id)
        except sqlite3.Error:
            account = None
        if account:
            plan_totals: dict[str, int] = {}
            open_invoices = [
                invoice
                for invoice in account["invoices"]
                if invoice["status"] in {"overdue", "open"}
                and int(invoice["outstanding_cents"]) > 0
            ]
            for invoice in open_invoices:
                plan = str(invoice["plan_name"])
                plan_totals[plan] = plan_totals.get(plan, 0) + int(
                    invoice["outstanding_cents"]
                )
            if plan_totals:
                breakdown = "; ".join(
                    f"{plan}: {balance.currency} {amount / 100:,.2f}"
                    for plan, amount in sorted(plan_totals.items())
                )
                response += f" El desglose por servicio es: {breakdown}."
            if open_invoices:
                invoice_lines = []
                for invoice in open_invoices:
                    due = date.fromisoformat(str(invoice["due_date"]))
                    invoice_lines.append(
                        f"{invoice['plan_name']} {invoice['billing_period']} "
                        f"(vence {due:%d/%m/%Y}): {balance.currency} "
                        f"{int(invoice['outstanding_cents']) / 100:,.2f}"
                    )
                response += " Facturas abiertas: " + "; ".join(invoice_lines) + "."
        response += " ¿Quieres revisar una fecha de pago?"
        return AgentResponse(
            response,
            intent,
            tool_calls=tool_calls,
            identity_validated=True,
        )

    def negotiation_response(
        self,
        text: str,
        client_id: str,
        balance: Balance,
        intent: Intent,
        tool_calls: list[ToolCall],
        *,
        previous_amount_cents: int | None = None,
        accept_bare_amount: bool = False,
        parsed_terms: tuple[int | None, date | None] | None = None,
    ) -> AgentResponse:
        amount, payment_date = parsed_terms or self.extract_payment_terms(
            text,
            balance.amount_cents,
            previous_amount_cents=previous_amount_cents,
            accept_bare_amount=accept_bare_amount,
        )
        if amount is None or payment_date is None:
            return self.negotiation_details_response(
                balance,
                intent,
                tool_calls,
                amount_cents=amount,
                payment_date=payment_date,
            )
        try:
            self.repository.save_pending_promise(client_id, amount, payment_date)
        except sqlite3.Error as error:
            return AgentResponse(
                "No pude guardar la propuesta por un problema temporal, así que aún no está pendiente. "
                "Inténtalo más tarde o pide ayuda a un asesor.",
                intent,
                tool_calls=tool_calls
                + [
                    ToolCall(
                        "guardar_propuesta_pago",
                        {
                            "cliente_id": client_id,
                            "monto": amount,
                            "fecha_pago": payment_date.isoformat(),
                        },
                        f"error: {type(error).__name__}",
                    )
                ],
                identity_validated=True,
            )
        return AgentResponse(
            f"Puedo proponer una promesa de pago por {balance.currency} {amount / 100:,.2f} "
            f"para el {payment_date:%d/%m/%Y}. Revísala y, si estás de acuerdo, responde exactamente "
            f"`{self._confirmation_command(amount, payment_date)}` para confirmarla.",
            intent,
            tool_calls=tool_calls
            + [
                ToolCall(
                    "guardar_propuesta_pago",
                    {
                        "cliente_id": client_id,
                        "monto": amount,
                        "fecha_pago": payment_date.isoformat(),
                    },
                    "success",
                )
            ],
            identity_validated=True,
        )

    @staticmethod
    def negotiation_details_response(
        balance: Balance,
        intent: Intent,
        tool_calls: list[ToolCall],
        *,
        amount_cents: int | None,
        payment_date: date | None,
    ) -> AgentResponse:
        if amount_cents is not None and payment_date is None:
            question = (
                f"¿Qué día exacto te sirve para proponer el pago de "
                f"{balance.currency} {amount_cents / 100:,.2f}?"
            )
        elif amount_cents is None and payment_date is not None:
            question = (
                f"¿Qué monto puedes pagar el {payment_date:%d/%m/%Y}? "
                f"Tu saldo pendiente es {balance.amount_display}."
            )
        else:
            question = (
                f"Tu saldo registrado es {balance.amount_display}. Para preparar una propuesta, "
                "dime qué monto puedes pagar y en qué fecha."
            )
        return AgentResponse(
            question,
            intent,
            tool_calls=tool_calls,
            identity_validated=True,
        )

    def confirm_promise_response(self, client_id: str, text: str) -> AgentResponse:
        try:
            pending = self.repository.get_pending_promise(client_id)
        except sqlite3.Error as error:
            return AgentResponse(
                "No pude revisar la propuesta por un problema temporal, así que no registré la promesa. "
                "Inténtalo más tarde o pide ayuda a un asesor.",
                Intent.CONFIRM_PROMISE,
                tool_calls=[
                    ToolCall(
                        "consultar_propuesta_pendiente",
                        {"cliente_id": client_id},
                        f"error: {type(error).__name__}",
                    )
                ],
                identity_validated=True,
            )
        if pending is None:
            return AgentResponse(
                "No encuentro una propuesta pendiente para confirmar. Si quieres, dime el monto y la fecha "
                "para preparar una.",
                Intent.CONFIRM_PROMISE,
                identity_validated=True,
            )
        amount, payment_date = pending
        if not self._is_explicit_confirmation(text, amount, payment_date):
            return AgentResponse(
                f"Aún no registré la promesa de {amount / 100:,.2f} para el "
                f"{payment_date:%d/%m/%Y}. Si estás de acuerdo, responde "
                f"`{self._confirmation_command(amount, payment_date)}`.",
                Intent.CONFIRM_PROMISE,
                identity_validated=True,
            )
        try:
            promise = self.payments.registrar_promesa_pago(client_id, amount, payment_date)
        except (ValueError, ClientNotFoundError, sqlite3.Error) as error:
            return AgentResponse(
                "No pude registrar la promesa en este momento; no quedó confirmada. "
                "Inténtalo más tarde o pide ayuda a un asesor.",
                Intent.CONFIRM_PROMISE,
                tool_calls=[
                    ToolCall(
                        "registrar_promesa_pago",
                        {
                            "cliente_id": client_id,
                            "monto": amount,
                            "fecha_pago": payment_date.isoformat(),
                        },
                        f"error: {type(error).__name__}",
                    )
                ],
                identity_validated=True,
            )
        return AgentResponse(
            f"Listo, registré tu promesa de pago por {promise.amount_cents / 100:,.2f} "
            f"para el {promise.payment_date:%d/%m/%Y}.",
            Intent.CONFIRM_PROMISE,
            tool_calls=[
                ToolCall(
                    "registrar_promesa_pago",
                    {
                        "cliente_id": client_id,
                        "monto": amount,
                        "fecha_pago": payment_date.isoformat(),
                    },
                    "success",
                )
            ],
            identity_validated=True,
        )

    def escalation_response(
        self,
        client_id: str | None,
        reason: str,
        summary: str,
        intent: Intent,
        *,
        prior_calls: list[ToolCall] | None = None,
    ) -> AgentResponse:
        calls = list(prior_calls or [])
        try:
            escalation = self.escalations.escalar_a_humano(client_id, reason, summary)
        except sqlite3.Error as error:
            calls.append(
                ToolCall(
                    "escalar_a_humano",
                    {"cliente_id": client_id, "motivo": reason},
                    f"error: {type(error).__name__}",
                )
            )
            return AgentResponse(
                "No pude enviar el caso a revisión automáticamente. No se registró el escalamiento; "
                "contacta a un asesor por el canal disponible.",
                intent,
                tool_calls=calls,
                escalated=False,
                identity_validated=client_id is not None,
            )
        calls.append(
            ToolCall("escalar_a_humano", {"cliente_id": client_id, "motivo": reason}, "success")
        )
        return AgentResponse(
            "Dejé tu caso en revisión con un asesor. No hice cambios en la cuenta. "
            f"Referencia: {escalation.escalation_id}.",
            intent,
            tool_calls=calls,
            escalated=True,
            identity_validated=client_id is not None,
        )

    def billing_failure_response(
        self, client_id: str, intent: Intent, tool_calls: list[ToolCall]
    ) -> AgentResponse:
        try:
            self.escalations.escalar_a_humano(
                client_id,
                "error_consulta_saldo",
                "Fallo al consultar saldo; no se informó un monto.",
            )
        except sqlite3.Error as error:
            return AgentResponse(
                "No pude consultar tu saldo ni enviar el caso a revisión. No inventaré un monto; "
                "contacta a un asesor.",
                intent,
                tool_calls=tool_calls
                + [
                    ToolCall(
                        "escalar_a_humano",
                        {"cliente_id": client_id},
                        f"error: {type(error).__name__}",
                    )
                ],
                escalated=False,
                identity_validated=True,
            )
        return AgentResponse(
            "No pude consultar tu saldo porque el sistema de facturación no responde. "
            "Dejé el caso con un asesor para revisión.",
            intent,
            tool_calls=tool_calls
            + [ToolCall("escalar_a_humano", {"cliente_id": client_id}, "success")],
            escalated=True,
            identity_validated=True,
        )

    def persist_response(
        self,
        response: AgentResponse,
        client_id: str | None,
        external_id: str | None,
        channel: str,
        *,
        message_id: str | None = None,
    ) -> AgentResponse:
        """Persist one outbound response; pass a stable ID when called from a replayable graph."""
        self.repository.record_message(
            message_id or f"{channel}-{uuid.uuid4().hex}",
            client_id,
            external_id,
            "outbound",
            response.text,
        )
        return response

    @staticmethod
    def _contains(text: str, terms: tuple[str, ...]) -> bool:
        lowered = text.casefold()
        return any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", lowered) for term in terms)

    @staticmethod
    def _is_explicit_confirmation(text: str, amount_cents: int, payment_date: date) -> bool:
        normalized = " ".join(text.casefold().split())
        command = CollectionsAgent._confirmation_command(amount_cents, payment_date)
        return normalized == command

    @staticmethod
    def _confirmation_command(amount_cents: int, payment_date: date) -> str:
        units, cents = divmod(amount_cents, 100)
        amount = f"{units}.{cents:02d}"
        return f"confirmo propuesta {amount} {payment_date.isoformat()}"

    @staticmethod
    def _is_confirmation_command(text: str) -> bool:
        normalized = " ".join(text.casefold().split())
        return CONFIRMATION_COMMAND.fullmatch(normalized) is not None

    def _classify(self, text: str, client_id: str | None) -> Intent:
        if self._contains(text, HUMAN_TERMS):
            return Intent.HUMAN
        if self._contains(text, DISCOUNT_TERMS):
            return Intent.DISCOUNT
        if self._contains(text, DISPUTE_TERMS):
            return Intent.DISPUTE
        if self._is_confirmation_command(text):
            return Intent.CONFIRM_PROMISE
        if self._is_account_details_question(text):
            return Intent.UNKNOWN
        if any(
            word in text.casefold()
            for word in ("promesa", "pagar", "pago", "fecha", "acuerdo", "negociar")
        ):
            return Intent.NEGOTIATE
        if any(
            word in text.casefold() for word in ("saldo", "debo", "factura", "cuánto", "cuanto")
        ):
            return Intent.BALANCE
        return Intent.UNKNOWN

    @classmethod
    def _is_account_details_question(cls, text: str) -> bool:
        """Route account-record questions to the SQL-backed customer-context tool."""
        return cls._contains(text, ACCOUNT_DETAIL_TERMS) and cls._contains(
            text, ACCOUNT_DETAIL_QUESTION_TERMS
        )

    @staticmethod
    def extract_payment_terms(
        text: str,
        default_amount: int,
        *,
        previous_amount_cents: int | None = None,
        accept_bare_amount: bool = False,
    ) -> tuple[int | None, date | None]:
        normalized = unicodedata.normalize("NFKD", text.casefold())
        normalized = "".join(
            char for char in normalized if not unicodedata.combining(char)
        )
        amount_match = re.search(
            r"(?:\b(?:balboas?|pab|usd)\s*|\$\s*)"
            r"(?P<prefix_amount>[\d,]+(?:\.\d{1,2})?|\d+,\d{1,2})"
            r"|(?P<suffix_amount>[\d,]+(?:\.\d{1,2})?|\d+,\d{1,2})"
            r"\s*(?:balboas?|pab|usd|\$)",
            normalized,
        )
        amount = (
            default_amount
            if "todo" in normalized or "total" in normalized
            else previous_amount_cents
        )
        if amount_match:
            raw = amount_match.group("prefix_amount") or amount_match.group("suffix_amount")
            raw = raw.replace(",", "") if "." in raw else raw.replace(",", ".")
            amount = round(float(raw) * 100)
        elif accept_bare_amount:
            bare_amount = re.search(
                r"^\s*(?:(?:yo\s+)?(?:puedo|podria|mejor|serian|seria)\s+)*"
                r"(?:(?:pagar|pago|abonar|abono|aportar)\s+)?"
                r"(?P<amount>\d+(?:[.,]\d{1,2})?)(?=\s|$)",
                normalized,
            )
            if bare_amount:
                raw = bare_amount.group("amount").replace(",", ".")
                amount = round(float(raw) * 100)

        date_match = re.search(
            r"(?P<year>20\d{2})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})"
            r"|(?P<local_day>\d{1,2})[-/](?P<local_month>\d{1,2})[-/]"
            r"(?P<local_year>20\d{2})",
            normalized,
        )
        if date_match:
            try:
                if date_match.group("year"):
                    year, month, day = (
                        int(date_match.group("year")),
                        int(date_match.group("month")),
                        int(date_match.group("day")),
                    )
                else:
                    year, month, day = (
                        int(date_match.group("local_year")),
                        int(date_match.group("local_month")),
                        int(date_match.group("local_day")),
                    )
                payment_date = date(year, month, day)
            except ValueError:
                payment_date = None
        else:
            relative = re.search(r"(\d+)\s*dias?", normalized)
            today = datetime.now(ZoneInfo("America/Bogota")).date()
            if relative:
                payment_date = today + timedelta(days=int(relative.group(1)))
            elif re.search(r"\bpasado manana\b", normalized):
                payment_date = today + timedelta(days=2)
            elif re.search(r"\bmanana\b", normalized):
                payment_date = today + timedelta(days=1)
            elif re.search(r"\bhoy\b", normalized):
                payment_date = today
            else:
                payment_date = None
                for weekday_name, weekday in WEEKDAYS.items():
                    if re.search(rf"\b{weekday_name}\b", normalized):
                        days_ahead = (weekday - today.weekday()) % 7
                        if days_ahead == 0 and "hoy" not in normalized:
                            days_ahead = 7
                        payment_date = today + timedelta(days=days_ahead)
                        break
        return amount, payment_date

    def _resolve_client(
        self,
        client_id: str | None,
        external_id: str | None,
    ) -> str | None:
        if client_id:
            return client_id.strip().upper()
        if external_id:
            row = self.repository.get_client_by_external_id(external_id)
            if row:
                return row["client_id"]
        return None
