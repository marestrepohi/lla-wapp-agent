"""LangGraph workflow for collection policy, customer memory, and knowledge/RAG chat."""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date
from typing import Any, Literal
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph import END, START, StateGraph

from lla_agent.application.agent import CollectionsAgent
from lla_agent.domain.models import AgentResponse, Balance, Intent, ToolCall
from lla_agent.persistence.sqlite import SQLiteRepository

from .agent import DEFAULT_MODEL, build_agent
from .memory import QdrantCustomerMemory, get_qdrant_memory, prepare_memory_context
from .models import build_chat_model
from .rag import WebCorpus
from .state import LLAAgentState
from .whatsapp_opening import classify_contact_confirmation

BUSINESS_INTENTS = {
    Intent.BALANCE,
    Intent.NEGOTIATE,
    Intent.CONFIRM_PROMISE,
    Intent.DISPUTE,
    Intent.DISCOUNT,
    Intent.HUMAN,
}
_LOGGER = logging.getLogger(__name__)
_EXPLICIT_CLIENT_ID = re.compile(
    r"\b(?:mi\s+)?(?:id|identificaci[oó]n|n[uú]mero)"
    r"(?:\s+(?:de|del)\s+cliente)?\s*(?:es|:|n[uú]mero)?\s*"
    r"(?:(?:el|la)\s+)?"
    r"(?P<client_id>C[A-Z0-9]{2,15}|[0-9]{3,12})\b",
    re.IGNORECASE,
)
_AWAITING_ID_REPLY = re.compile(
    r"^(?:(?:s[ií]|claro|correcto|as[ií]\s+es|soy\s+yo|es|ser[ií]a)"
    r"[\s,.:;-]*)?"
    r"(?:(?:(?:mi\s+)?(?:id|identificaci[oó]n|n[uú]mero)"
    r"(?:\s+(?:de|del)\s+cliente)?\s*(?:es|:)?)[\s,.:;-]*)?"
    r"(?:(?:el|la)\s+)?(?:n[uú]mero\s+)?"
    r"(?P<client_id>C[A-Z0-9]{2,15}|[0-9]{3,12})"
    r"[\s.!?,]*(?:gracias)?[\s.!?]*$",
    re.IGNORECASE,
)
SUMMARY_TRIGGER_MESSAGES = 20
SUMMARY_RETAIN_MESSAGES = 8
PAYMENT_DETAIL_STAGES = frozenset({"awaiting_details", "awaiting_amount", "awaiting_date"})
_SUMMARY_UNSAFE_TERMS = re.compile(
    r"\b(?:nombre|tel[eé]fono|correo|direcci[oó]n|identificador|id|saldo|deuda|monto|dinero|"
    r"pago|pagar|pag[oó]|cuota|vencimiento|factura|cuenta|tarjeta|credencial|contrase[nñ]a|"
    r"c[oó]digo|token|financier[oa]|debt|balance|payment|account|credit card|password|"
    r"credential|verification code)\b",
    re.IGNORECASE,
)
_SUMMARY_CAPITALIZED_WORD = re.compile(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}\b")
_SUMMARY_SAFE_SENTENCE_STARTERS = frozenset(
    {
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "prefiere",
        "prefieren",
        "prefiero",
        "solicita",
        "solicitan",
        "solicito",
        "valora",
        "valoran",
        "usa",
        "utiliza",
        "necesita",
        "necesitan",
        "busca",
        "buscan",
        "quiere",
        "quieren",
        "mantiene",
        "requiere",
        "evita",
        "acepta",
        "se",
        "tiene",
        "le",
        "su",
        "contexto",
        "conversación",
        "usuario",
        "persona",
        "me",
        "mi",
        "para",
        "es",
        "está",
        "puede",
        "pueden",
    }
)


def _message_text(state: LLAAgentState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            content = message.content
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                return " ".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
            return str(content).strip()
    return ""


def _qa_client_id_claim(text: str, *, awaiting_client_id: bool) -> str | None:
    explicit = _EXPLICIT_CLIENT_ID.search(text)
    if explicit:
        return explicit.group("client_id").strip().upper()
    if not awaiting_client_id:
        return None
    match = _AWAITING_ID_REPLY.fullmatch(text.strip())
    if match:
        return match.group("client_id").strip().upper()
    return None


def _response_metadata(response: AgentResponse) -> dict[str, Any]:
    return {
        "intent": response.intent.value,
        "tool_calls": [
            {
                "name": call.name,
                "arguments": call.arguments,
                "outcome": call.outcome,
            }
            for call in response.tool_calls
        ],
        "escalated": response.escalated,
        "opted_out": response.opted_out,
        "identity_validated": response.identity_validated,
    }


def _tool_calls(state: LLAAgentState) -> list[ToolCall]:
    return [
        ToolCall(
            name=str(item.get("name", "")),
            arguments=dict(item.get("arguments", {})),
            outcome=str(item.get("outcome", "unknown")),
        )
        for item in state.get("tool_calls", [])
    ]


def _message_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def _safe_for_summary(text: str) -> bool:
    """Reject likely PII, account details, numbers, and proper names conservatively."""
    if not text.strip() or _SUMMARY_UNSAFE_TERMS.search(text):
        return False
    if re.search(r"\b(?:soy|me llamo|my name is|i am)\s+\w+", text, re.IGNORECASE):
        return False
    if re.search(r"\d|[$€£@]|https?://|\b[A-Z]{2,}\b", text):
        return False

    for match in _SUMMARY_CAPITALIZED_WORD.finditer(text):
        prefix = text[: match.start()].rstrip()
        starts_sentence = not prefix or prefix[-1] in ".!?\n:•-*"
        if not starts_sentence or match.group().casefold() not in _SUMMARY_SAFE_SENTENCE_STARTERS:
            return False
    return True


def _normalize_plain_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _looks_like_payment_details(text: str) -> bool:
    amount, payment_date = CollectionsAgent.extract_payment_terms(
        text, 0, accept_bare_amount=True
    )
    if (amount is not None and amount > 0) or payment_date is not None:
        return True
    normalized = _normalize_plain_text(text).strip(" .,!¿?;:")
    return bool(
        re.fullmatch(
            r"(?:(?:para|en)\s+)?(?:la\s+)?"
            r"(?:(?:otra|proxima|siguiente)\s+)?semana",
            normalized,
        )
    )


def _prior_assistant_text(messages: list[Any], current_message_id: str) -> str:
    current_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], HumanMessage)
            and getattr(messages[index], "id", None) == current_message_id
        ),
        len(messages),
    )
    for message in reversed(messages[:current_index]):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return _message_content(message)
    return ""


def _recover_payment_stage(messages: list[Any], message_id: str) -> dict[str, Any]:
    """Recover a partially completed legacy payment flow from the preceding reply."""
    previous_reply = _prior_assistant_text(messages, message_id)
    normalized = _normalize_plain_text(previous_reply)
    if not previous_reply:
        return {}

    amount, payment_date = CollectionsAgent.extract_payment_terms(previous_reply, 0)
    if amount and re.search(r"\bque dia exacto\b|\bdia exacto\b", normalized):
        return {
            "payment_flow_stage": "awaiting_date",
            "pending_payment_amount_cents": amount,
            "pending_payment_date": None,
        }
    if payment_date and re.search(r"\bque monto\b|\bque cantidad\b", normalized):
        return {
            "payment_flow_stage": "awaiting_amount",
            "pending_payment_amount_cents": None,
            "pending_payment_date": payment_date.isoformat(),
        }
    if re.search(r"\bmonto\b", normalized) and re.search(r"\bfecha\b", normalized):
        return {
            "payment_flow_stage": "awaiting_details",
            "pending_payment_amount_cents": None,
            "pending_payment_date": None,
        }
    return {}


def _balance_from_state(state: LLAAgentState) -> Balance:
    value = state["balance"]
    due_value = value.get("due_date")
    return Balance(
        cliente_id=str(value["cliente_id"]),
        amount_cents=int(value["amount_cents"]),
        currency=str(value["currency"]),
        due_date=date.fromisoformat(str(due_value)) if due_value else None,
        status=str(value["status"]),
    )


def build_workflow(
    *,
    repository: SQLiteRepository,
    checkpointer: Any,
    model_name: str = DEFAULT_MODEL,
    model: BaseChatModel | None = None,
    web_corpus: WebCorpus | None = None,
    long_term_memory: QdrantCustomerMemory | None = None,
    qa_id_linking: bool = False,
):
    """Build the root graph; its checkpointer owns the whole conversation thread."""
    collections = CollectionsAgent(repository)
    web_corpus = web_corpus or WebCorpus()
    chat_model = model or build_chat_model(model_name)
    knowledge_agent = build_agent(
        repository=repository,
        model_name=model_name,
        model=chat_model,
        web_corpus=web_corpus,
    )

    def is_qa_identity_channel(state: LLAAgentState) -> bool:
        channel = str(state.get("channel", ""))
        external_id = str(state.get("external_id", ""))
        return qa_id_linking and (
            (channel == "whatsapp" and external_id.startswith("whatsapp:"))
            or (channel == "chainlit" and external_id.startswith("chainlit:"))
        )

    def intake(state: LLAAgentState) -> dict[str, Any]:
        text = _message_text(state)
        channel = str(state.get("channel", "local"))
        message_id = str(state.get("message_id") or f"{channel}-{uuid4().hex}")
        client_id = state.get("client_id")
        external_id = state.get("external_id")

        opening_context = state.get("opening_context", {})
        if not isinstance(opening_context, dict):
            opening_context = {}
        opening_name = str(opening_context.get("contact_name", "")).strip()
        opening_stage = str(state.get("opening_stage", "") or "")
        if (
            opening_context.get("flow") == "contact_confirmation"
            and not opening_stage
        ):
            opening_stage = "awaiting_contact_confirmation"

        def turn_update(
            resolved_id: str | None,
            *,
            route: str,
            response: str = "",
            stage: str | None = None,
            awaiting_id: bool = False,
            notice: str = "",
            response_meta: dict[str, Any] | None = None,
            tool_calls: list[ToolCall] | None = None,
            balance: Balance | None = None,
            billing_failed: bool = False,
            identity_validated: bool = False,
        ) -> dict[str, Any]:
            recorded_id = collections.begin_turn(
                text,
                client_id=resolved_id,
                external_id=external_id,
                channel=channel,
                message_id=message_id,
            )
            result: dict[str, Any] = {
                "input_text": text,
                "message_id": message_id,
                "client_id": recorded_id,
                "route": route,
                "response_text": response,
                "response_meta": response_meta or {},
                "awaiting_client_id": awaiting_id,
                "qa_profile_notice": notice,
                "identity_validated": identity_validated,
                "opted_out": False,
                "escalated": False,
                "tool_calls": [
                    {"name": call.name, "arguments": call.arguments, "outcome": call.outcome}
                    for call in (tool_calls or [])
                ],
                "balance": {},
                "billing_failed": billing_failed,
            }
            if stage:
                result["opening_stage"] = stage
            if balance:
                result["balance"] = {
                    "cliente_id": balance.cliente_id,
                    "amount_cents": balance.amount_cents,
                    "currency": balance.currency,
                    "due_date": balance.due_date.isoformat() if balance.due_date else None,
                    "status": balance.status,
                }
            return result

        def link_and_summarize(claimed_id: str, stage: str | None) -> dict[str, Any]:
            linked_profile = repository.get_client_by_external_id(str(external_id))
            existing_profile = repository.get_client(claimed_id)
            if existing_profile is None and linked_profile is None:
                try:
                    repository.ensure_qa_client(claimed_id)
                except (ValueError, sqlite3.Error):
                    return turn_update(
                        linked_profile["client_id"] if linked_profile else None,
                        route="early_response",
                        response=(
                            "No pude ubicar ese ID. Revísalo y vuelve a enviarlo, por favor."
                        ),
                        stage="awaiting_client_id" if stage else None,
                        awaiting_id=True,
                    )

            link_status = repository.link_client_to_external_id(claimed_id, str(external_id))
            if link_status == "conflict":
                resolved_id = str(linked_profile["client_id"]) if linked_profile else None
            else:
                resolved_id = claimed_id
            if link_status == "conflict":
                return turn_update(
                    resolved_id,
                    route="early_response",
                    response=(
                        "No pude asociar este número con ese ID. Revísalo o pide ayuda a un asesor."
                    ),
                    stage=stage or "complete",
                )
            if link_status not in {"linked", "already_linked"}:
                return turn_update(
                    None,
                    route="early_response",
                    response="No pude asociar ese ID. Revísalo e inténtalo de nuevo, por favor.",
                    stage="awaiting_client_id" if stage else None,
                    awaiting_id=True,
                )

            simulated = repository.is_qa_simulated_client(claimed_id)
            balance, calls = collections.lookup_balance(claimed_id)
            if balance is None:
                response = collections.billing_failure_response(
                    claimed_id, Intent.BALANCE, calls
                )
                overview = response.text
            else:
                response = collections.balance_response(balance, Intent.BALANCE, calls)
                overview = response.text
            greeting_name = opening_name or ""
            greeting = f"Gracias, {greeting_name}. " if greeting_name else "Gracias. "
            answer = f"{greeting}Ya ubiqué el registro {claimed_id}. {overview}"
            notice = (
                "Este expediente se generó para la prueba; sus importes son simulados."
                if simulated
                else ""
            )
            response = AgentResponse(
                answer,
                Intent.BALANCE,
                tool_calls=response.tool_calls,
                escalated=response.escalated,
                identity_validated=True,
            )
            return turn_update(
                claimed_id,
                route="early_response",
                response=answer,
                stage="complete" if stage else None,
                awaiting_id=False,
                notice=notice,
                response_meta=_response_metadata(response),
                tool_calls=response.tool_calls,
                balance=balance,
                billing_failed=balance is None,
                identity_validated=True,
            )

        if opening_stage == "contact_mismatch":
            return turn_update(
                client_id,
                route="early_response",
                response=(
                    "Entendido. No consultaré ni compartiré información de cuenta por este número."
                ),
                stage="contact_mismatch",
            )

        if is_qa_identity_channel(state) and external_id and opening_stage in {
            "awaiting_contact_confirmation",
            "awaiting_client_id",
        }:
            if opening_stage == "awaiting_contact_confirmation":
                confirmation = classify_contact_confirmation(text, opening_name)
                if confirmation == "no":
                    return turn_update(
                        client_id,
                        route="early_response",
                        response=(
                            "Disculpa la molestia. No consultaré ni compartiré datos de cuenta "
                            "por este número. Gracias por avisarme."
                        ),
                        stage="contact_mismatch",
                    )
                if confirmation == "yes":
                    claimed_id = _qa_client_id_claim(text, awaiting_client_id=True)
                    if claimed_id:
                        return link_and_summarize(claimed_id, "complete")
                    name = f" {opening_name}" if opening_name else ""
                    return turn_update(
                        client_id,
                        route="early_response",
                        response=(
                            f"Gracias{name}. Para ubicar el expediente correcto, ¿me compartes "
                            "tu ID de cliente?"
                        ),
                        stage="awaiting_client_id",
                        awaiting_id=True,
                    )
                name = f" {opening_name}" if opening_name else ""
                return turn_update(
                    client_id,
                    route="early_response",
                    response=(
                        "Hola, soy el asistente virtual de LLA. Te escribo por una consulta de "
                        f"servicio. ¿Hablo con{name}?"
                    ),
                    stage="awaiting_contact_confirmation",
                )

            claimed_id = _qa_client_id_claim(text, awaiting_client_id=True)
            if claimed_id:
                return link_and_summarize(claimed_id, "complete")
            return turn_update(
                client_id,
                route="early_response",
                response="Cuando puedas, compárteme tu ID de cliente y reviso el estado de la cuenta.",
                stage="awaiting_client_id",
                awaiting_id=True,
            )

        if is_qa_identity_channel(state) and external_id:
            claimed_id = _qa_client_id_claim(
                text,
                awaiting_client_id=(
                    bool(state.get("awaiting_client_id", False))
                    or repository.get_client_by_external_id(external_id) is None
                ),
            )
            if claimed_id:
                return link_and_summarize(claimed_id, opening_stage or None)

        resolved_id = collections.begin_turn(
            text,
            client_id=client_id,
            external_id=external_id,
            channel=channel,
            message_id=message_id,
        )
        payment_stage = str(state.get("payment_flow_stage", "") or "")
        payment_amount = state.get("pending_payment_amount_cents")
        payment_date = state.get("pending_payment_date")
        if not payment_stage:
            recovered = _recover_payment_stage(
                list(state.get("messages", [])), message_id
            )
            payment_stage = str(recovered.get("payment_flow_stage", ""))
            payment_amount = recovered.get("pending_payment_amount_cents")
            payment_date = recovered.get("pending_payment_date")
        return {
            "input_text": text,
            "message_id": message_id,
            "client_id": resolved_id,
            "route": "opt_out",
            "response_text": "",
            "response_meta": {},
            "qa_profile_notice": "",
            "identity_validated": False,
            "opted_out": False,
            "escalated": False,
            "tool_calls": [],
            "balance": {},
            "billing_failed": False,
            "payment_flow_stage": payment_stage,
            "pending_payment_amount_cents": payment_amount,
            "pending_payment_date": payment_date,
        }

    def opt_out_gate(state: LLAAgentState) -> dict[str, Any]:
        response = collections.opt_out_response(
            str(state.get("input_text", "")),
            state.get("client_id"),
            state.get("external_id"),
        )
        if response is not None:
            metadata = _response_metadata(response)
            return {
                "route": "early_response",
                "response_text": response.text,
                "response_meta": metadata,
                "opted_out": response.opted_out,
                "identity_validated": response.identity_validated,
                "payment_flow_stage": "",
                "pending_payment_amount_cents": None,
                "pending_payment_date": None,
            }
        if state.get("route") == "early_response":
            return {}
        return {"route": "classify"}

    def route_after_opt_out(state: LLAAgentState) -> Literal["classify_intent", "finalize"]:
        return "finalize" if state.get("route") == "early_response" else "classify_intent"

    def classify_intent(state: LLAAgentState) -> dict[str, Any]:
        text = str(state.get("input_text", ""))
        intent = collections.classify_intent(text, state.get("client_id"))
        stage = str(state.get("payment_flow_stage", "") or "")
        if stage in PAYMENT_DETAIL_STAGES and intent == Intent.UNKNOWN:
            if _looks_like_payment_details(text):
                intent = Intent.NEGOTIATE
        if stage == "awaiting_confirmation" and intent == Intent.UNKNOWN:
            normalized = _normalize_plain_text(text).strip(" .,!¿?;:")
            if normalized in {"si", "claro", "de acuerdo", "acepto", "confirmo", "ok", "dale"}:
                intent = Intent.CONFIRM_PROMISE
        route = "business" if intent in BUSINESS_INTENTS else "knowledge"
        update: dict[str, Any] = {"intent": intent.value, "route": route}
        if intent not in {Intent.NEGOTIATE, Intent.CONFIRM_PROMISE}:
            update.update(
                {
                    "payment_flow_stage": "",
                    "pending_payment_amount_cents": None,
                    "pending_payment_date": None,
                }
            )
        return update

    def route_after_intent(state: LLAAgentState) -> Literal["identity_gate", "prepare_memory"]:
        return "identity_gate" if state.get("route") == "business" else "prepare_memory"

    def identity_gate(state: LLAAgentState) -> dict[str, Any]:
        if is_qa_identity_channel(state) and state.get("client_id") is None:
            return {
                "route": "blocked",
                "response_text": "Claro. Para ubicar tu expediente, ¿me compartes tu ID de cliente?",
                "response_meta": {"intent": Intent.UNKNOWN.value},
                "awaiting_client_id": True,
            }
        response = collections.validate_identity(
            state.get("client_id"), state.get("external_id")
        )
        if response is None:
            return {"identity_validated": True}
        return {
            "route": "blocked",
            "response_text": response.text,
            "response_meta": _response_metadata(response),
        }

    def route_after_identity(
        state: LLAAgentState,
    ) -> Literal["read_balance", "confirm_promise", "escalate", "finalize"]:
        if not state.get("identity_validated"):
            return "finalize"
        intent = Intent(str(state.get("intent", Intent.UNKNOWN.value)))
        if intent == Intent.CONFIRM_PROMISE:
            return "confirm_promise"
        if intent in {Intent.HUMAN, Intent.DISCOUNT}:
            return "escalate"
        return "read_balance"

    def read_balance(state: LLAAgentState) -> dict[str, Any]:
        client_id = str(state["client_id"])
        balance, calls = collections.lookup_balance(client_id)
        update: dict[str, Any] = {
            "tool_calls": [
                {"name": call.name, "arguments": call.arguments, "outcome": call.outcome}
                for call in calls
            ],
            "billing_failed": balance is None,
            "balance": {},
        }
        if balance is not None:
            update["balance"] = {
                "cliente_id": balance.cliente_id,
                "amount_cents": balance.amount_cents,
                "currency": balance.currency,
                "due_date": balance.due_date.isoformat() if balance.due_date else None,
                "status": balance.status,
            }
        return update

    def route_after_balance(
        state: LLAAgentState,
    ) -> Literal["billing_failure", "escalate", "respond_balance", "prepare_promise"]:
        if state.get("billing_failed"):
            return "billing_failure"
        if state.get("intent") == Intent.DISPUTE.value:
            return "escalate"
        if state.get("intent") == Intent.BALANCE.value:
            return "respond_balance"
        return "prepare_promise"

    def respond_balance(state: LLAAgentState) -> dict[str, Any]:
        response = collections.balance_response(
            _balance_from_state(state), Intent(str(state["intent"])), _tool_calls(state)
        )
        return {"response_text": response.text, "response_meta": _response_metadata(response)}

    def prepare_promise(state: LLAAgentState) -> dict[str, Any]:
        text = str(state.get("input_text", ""))
        balance = _balance_from_state(state)
        stage = str(state.get("payment_flow_stage", "") or "")
        previous_amount = state.get("pending_payment_amount_cents")
        previous_date_value = state.get("pending_payment_date")
        terms = CollectionsAgent.extract_payment_terms(
            text,
            balance.amount_cents,
            previous_amount_cents=(
                int(previous_amount) if previous_amount is not None else None
            ),
            accept_bare_amount=True,
        )
        amount, payment_date = terms
        if payment_date is None and previous_date_value:
            try:
                payment_date = date.fromisoformat(str(previous_date_value))
            except ValueError:
                payment_date = None
        terms = (amount, payment_date)
        response = collections.negotiation_response(
            text,
            str(state["client_id"]),
            balance,
            Intent(str(state["intent"])),
            _tool_calls(state),
            parsed_terms=terms,
        )
        stage_update = "awaiting_details"
        if amount is not None and payment_date is None:
            stage_update = "awaiting_date"
        elif amount is None and payment_date is not None:
            stage_update = "awaiting_amount"
        elif amount is not None and payment_date is not None:
            proposal_saved = any(
                call.name == "guardar_propuesta_pago" and call.outcome == "success"
                for call in response.tool_calls
            )
            stage_update = "awaiting_confirmation" if proposal_saved else ""
        return {
            "response_text": response.text,
            "response_meta": _response_metadata(response),
            "payment_flow_stage": stage_update,
            "pending_payment_amount_cents": amount,
            "pending_payment_date": payment_date.isoformat() if payment_date else None,
        }

    def confirm_promise(state: LLAAgentState) -> dict[str, Any]:
        response = collections.confirm_promise_response(
            str(state["client_id"]), str(state.get("input_text", ""))
        )
        confirmed = any(
            call.name == "registrar_promesa_pago" and call.outcome == "success"
            for call in response.tool_calls
        )
        return {
            "response_text": response.text,
            "response_meta": _response_metadata(response),
            "payment_flow_stage": "" if confirmed else state.get("payment_flow_stage", ""),
            "pending_payment_amount_cents": None if confirmed else state.get("pending_payment_amount_cents"),
            "pending_payment_date": None if confirmed else state.get("pending_payment_date"),
        }

    def escalate(state: LLAAgentState) -> dict[str, Any]:
        intent = Intent(str(state["intent"]))
        client_id = str(state["client_id"])
        if intent == Intent.DISPUTE:
            balance = _balance_from_state(state)
            reason = "saldo_disputado"
            summary = (
                f"El cliente disputa el saldo consultado ({balance.amount_display}); "
                "requiere revisión humana."
            )
            prior_calls = _tool_calls(state)
        elif intent == Intent.DISCOUNT:
            reason = "descuento_no_autorizado"
            summary = "El cliente solicita un descuento o condonación; no se aplicó ningún ajuste."
            prior_calls = []
        else:
            reason = "solicitud_humana"
            summary = str(state.get("input_text", ""))
            prior_calls = []
        response = collections.escalation_response(
            client_id, reason, summary, intent, prior_calls=prior_calls
        )
        return {"response_text": response.text, "response_meta": _response_metadata(response)}

    def billing_failure(state: LLAAgentState) -> dict[str, Any]:
        response = collections.billing_failure_response(
            str(state["client_id"]), Intent(str(state["intent"])), _tool_calls(state)
        )
        return {
            "response_text": response.text,
            "response_meta": _response_metadata(response),
            "escalated": response.escalated,
        }

    def prepare_memory(state: LLAAgentState) -> dict[str, Any]:
        client_id = state.get("client_id")
        if not client_id or repository.get_client(client_id) is None:
            return {"memory_context": ""}
        memory = long_term_memory
        if memory is None:
            try:
                memory = get_qdrant_memory()
            except Exception as error:
                _LOGGER.warning("Long-term memory unavailable: %s", type(error).__name__)
                return {"memory_context": ""}
        if memory is None:
            return {"memory_context": ""}
        context = prepare_memory_context(
            memory,
            client_id=client_id,
            user_message=str(state.get("input_text", "")),
            model_name=model_name,
            model=chat_model,
        )
        return {"memory_context": context}

    def finalize(state: LLAAgentState) -> dict[str, Any]:
        channel = str(state.get("channel", "local"))
        client_id = state.get("client_id")
        external_id = state.get("external_id")
        message_id = str(state.get("message_id") or uuid4().hex)
        is_knowledge_route = state.get("route") == "knowledge"
        answer = str(state.get("response_text", ""))
        if is_knowledge_route:
            for message in reversed(state.get("messages", [])):
                if isinstance(message, AIMessage) and not message.tool_calls:
                    answer = _message_content(message)
                    break
        if not answer:
            answer = "No pude generar una respuesta. Un asesor puede ayudarte."

        qa_notice = str(state.get("qa_profile_notice", "") or "").strip()
        if qa_notice:
            answer = f"{qa_notice}\n\n{answer}"

        meta = state.get("response_meta", {})
        intent_value = str(meta.get("intent", state.get("intent", Intent.UNKNOWN.value)))
        try:
            intent = Intent(intent_value)
        except ValueError:
            intent = Intent.UNKNOWN
        response = AgentResponse(
            answer,
            intent,
            escalated=bool(meta.get("escalated", state.get("escalated", False))),
            opted_out=bool(meta.get("opted_out", state.get("opted_out", False))),
            identity_validated=bool(
                meta.get("identity_validated", state.get("identity_validated", False))
            ),
        )
        collections.persist_response(
            response,
            client_id,
            external_id,
            channel,
            message_id=f"{channel}:{message_id}:outbound",
        )
        update: dict[str, Any] = {"response_text": answer}
        if not is_knowledge_route:
            update["messages"] = AIMessage(content=answer)
        return update

    def route_after_finalize(state: LLAAgentState) -> Literal["summarize_history", "__end__"]:
        if state.get("opted_out"):
            return END
        return (
            "summarize_history"
            if len(state.get("messages", [])) > SUMMARY_TRIGGER_MESSAGES
            else END
        )

    def summarize_history(state: LLAAgentState) -> dict[str, Any]:
        messages = list(state.get("messages", []))
        cutoff = max(0, len(messages) - SUMMARY_RETAIN_MESSAGES)
        while cutoff > 0 and not isinstance(messages[cutoff], HumanMessage):
            cutoff -= 1
        if cutoff == 0:
            return {}

        older_messages = messages[:cutoff]
        if any(message.id is None for message in older_messages):
            _LOGGER.warning("Skipping conversation compaction because a message has no ID")
            return {}
        previous_summary = str(state.get("summary", "") or "").strip()
        prior_summary_is_safe = not previous_summary or _safe_for_summary(previous_summary)
        prior_summary = previous_summary if prior_summary_is_safe else ""
        safe_messages = [
            message
            for message in older_messages
            if _safe_for_summary(_message_content(message))
        ]
        removals = [RemoveMessage(id=message.id) for message in older_messages]
        if str(state.get("channel", "")) == "whatsapp":
            # A WhatsApp reply must not wait on a second Groq call for history maintenance.
            deterministic_lines = [
                f"{'Cliente' if isinstance(message, HumanMessage) else 'Asistente'}: "
                f"{_message_content(message).strip()}"
                for message in safe_messages[-6:]
            ]
            summary_parts = [prior_summary, *deterministic_lines]
            summary = "\n".join(part for part in summary_parts if part)[-1200:]
            return {"summary": summary, "messages": removals}
        if not safe_messages:
            update: dict[str, Any] = {
                "messages": removals
            }
            if not prior_summary_is_safe:
                update["summary"] = ""
            return update
        transcript = "\n".join(
            f"{message.type}: {_message_content(message)}" for message in safe_messages
        )
        prompt = (
            "Actualiza un resumen fiel y breve de una conversación de soporte. "
            "Usa solo los mensajes seguros entregados abajo; se omitieron turnos que pudieran "
            "contener datos personales o financieros. Usa solo hechos expresados por el usuario "
            "y respuestas dadas por el agente. "
            "No guardes nombres, identificadores, teléfonos, correos, saldos, deudas, montos, "
            "fechas de pago, credenciales ni datos sensibles. No inventes ni resuelvas hechos "
            "financieros; esos datos se consultan en el sistema de facturación. Conserva solo "
            "preferencias no sensibles, contexto general y asuntos no financieros pendientes.\n\n"
            f"Resumen anterior:\n{prior_summary or '(vacío)'}\n\n"
            f"Mensajes antiguos completos:\n{transcript}"
        )
        try:
            result = chat_model.invoke(prompt)
        except Exception as error:
            _LOGGER.warning("Conversation summary failed: %s", type(error).__name__)
            return {"summary": ""} if not prior_summary_is_safe else {}

        summary = _message_content(result).strip()
        if not summary or not _safe_for_summary(summary):
            _LOGGER.warning("Conversation summary rejected by the local safety filter")
            return {"summary": ""} if not prior_summary_is_safe else {}
        return {"summary": summary, "messages": removals}

    builder = StateGraph(LLAAgentState)
    builder.add_node("intake", intake)
    builder.add_node("opt_out_gate", opt_out_gate)
    builder.add_node("classify_intent", classify_intent)
    builder.add_node("identity_gate", identity_gate)
    builder.add_node("read_balance", read_balance)
    builder.add_node("respond_balance", respond_balance)
    builder.add_node("prepare_promise", prepare_promise)
    builder.add_node("confirm_promise", confirm_promise)
    builder.add_node("escalate", escalate)
    builder.add_node("billing_failure", billing_failure)
    builder.add_node("prepare_memory", prepare_memory)
    builder.add_node("knowledge_agent", knowledge_agent)
    builder.add_node("finalize", finalize)
    builder.add_node("summarize_history", summarize_history)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "opt_out_gate")
    builder.add_conditional_edges(
        "opt_out_gate",
        route_after_opt_out,
        {"classify_intent": "classify_intent", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "classify_intent",
        route_after_intent,
        {"identity_gate": "identity_gate", "prepare_memory": "prepare_memory"},
    )
    builder.add_conditional_edges(
        "identity_gate",
        route_after_identity,
        {
            "read_balance": "read_balance",
            "confirm_promise": "confirm_promise",
            "escalate": "escalate",
            "finalize": "finalize",
        },
    )
    builder.add_conditional_edges(
        "read_balance",
        route_after_balance,
        {
            "billing_failure": "billing_failure",
            "escalate": "escalate",
            "respond_balance": "respond_balance",
            "prepare_promise": "prepare_promise",
        },
    )
    for node in (
        "respond_balance",
        "prepare_promise",
        "confirm_promise",
        "escalate",
        "billing_failure",
        "knowledge_agent",
    ):
        builder.add_edge(node, "finalize")
    builder.add_edge("prepare_memory", "knowledge_agent")
    builder.add_conditional_edges(
        "finalize",
        route_after_finalize,
        {"summarize_history": "summarize_history", END: END},
    )
    builder.add_edge("summarize_history", END)
    return builder.compile(checkpointer=checkpointer)
