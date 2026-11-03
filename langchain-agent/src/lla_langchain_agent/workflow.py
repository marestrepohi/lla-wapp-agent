"""LangGraph workflow for collection policy, customer memory, and knowledge/RAG chat."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Literal
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph

from lla_agent.application.agent import CollectionsAgent
from lla_agent.domain.models import AgentResponse, Balance, Intent, ToolCall
from lla_agent.persistence.sqlite import SQLiteRepository

from .agent import DEFAULT_MODEL, build_agent
from .memory import QdrantCustomerMemory, get_qdrant_memory, prepare_memory_context
from .rag import WebCorpus
from .state import LLAAgentState

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
    r"\b(?:mi\s+)?(?:id|identificaci[oó]n|n[uú]mero\s+(?:de|del)\s+cliente)"
    r"(?:\s+(?:de|del)\s+cliente)?\s*(?:es|:|n[uú]mero)?\s*"
    r"(?P<client_id>[A-Za-z0-9-]{3,20})\b",
    re.IGNORECASE,
)
_BARE_CLIENT_ID = re.compile(r"(?:C[A-Z0-9]{2,15}|[0-9]{3,12})", re.IGNORECASE)
SUMMARY_TRIGGER_MESSAGES = 20
SUMMARY_RETAIN_MESSAGES = 8
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
    normalized = text.strip()
    if awaiting_client_id and _BARE_CLIENT_ID.fullmatch(normalized):
        return normalized.upper()
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
    chat_model = model or ChatGroq(model=model_name, temperature=0)
    knowledge_agent = build_agent(
        repository=repository,
        model_name=model_name,
        model=chat_model,
        web_corpus=web_corpus,
    )

    def is_qa_whatsapp(state: LLAAgentState) -> bool:
        return (
            qa_id_linking
            and str(state.get("channel", "")) == "whatsapp"
            and str(state.get("external_id", "")).startswith("whatsapp:")
        )

    def intake(state: LLAAgentState) -> dict[str, Any]:
        text = _message_text(state)
        channel = str(state.get("channel", "local"))
        message_id = str(state.get("message_id") or f"{channel}-{uuid4().hex}")
        client_id = state.get("client_id")
        external_id = state.get("external_id")

        if is_qa_whatsapp(state) and external_id:
            claimed_id = _qa_client_id_claim(
                text, awaiting_client_id=bool(state.get("awaiting_client_id", False))
            )
            if claimed_id:
                link_status = repository.link_client_to_external_id(claimed_id, external_id)
                resolved_id = collections.begin_turn(
                    text,
                    client_id=client_id,
                    external_id=external_id,
                    channel=channel,
                    message_id=message_id,
                )
                if link_status in {"linked", "already_linked"}:
                    reply = (
                        f"Listo. En esta demo asocié este WhatsApp al perfil de prueba "
                        f"{claimed_id}. Ya puedes consultar sus datos ficticios."
                    )
                    awaiting = False
                elif link_status == "conflict":
                    reply = (
                        "Este WhatsApp ya está asociado a otro perfil de prueba y no puedo "
                        "cambiarlo desde el chat. Quien administra la demo puede borrar la "
                        "asociación local y volver a intentarlo."
                    )
                    awaiting = False
                else:
                    reply = (
                        f"No encontré el perfil de prueba {claimed_id}. Puedes intentar con "
                        "1234, 5678 o 9012, o con uno de los perfiles C001, C002 y C003."
                    )
                    awaiting = True
                return {
                    "input_text": text,
                    "message_id": message_id,
                    "client_id": resolved_id,
                    "route": "early_response",
                    "response_text": reply,
                    "response_meta": {"intent": Intent.UNKNOWN.value},
                    "awaiting_client_id": awaiting,
                    "identity_validated": False,
                    "opted_out": False,
                    "escalated": False,
                    "tool_calls": [],
                    "balance": {},
                    "billing_failed": False,
                }

        resolved_id = collections.begin_turn(
            text,
            client_id=client_id,
            external_id=external_id,
            channel=channel,
            message_id=message_id,
        )
        return {
            "input_text": text,
            "message_id": message_id,
            "client_id": resolved_id,
            "route": "opt_out",
            "response_text": "",
            "response_meta": {},
            "identity_validated": False,
            "opted_out": False,
            "escalated": False,
            "tool_calls": [],
            "balance": {},
            "billing_failed": False,
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
            }
        if state.get("route") == "early_response":
            return {}
        return {"route": "classify"}

    def route_after_opt_out(state: LLAAgentState) -> Literal["classify_intent", "finalize"]:
        return "finalize" if state.get("route") == "early_response" else "classify_intent"

    def classify_intent(state: LLAAgentState) -> dict[str, Any]:
        intent = collections.classify_intent(
            str(state.get("input_text", "")), state.get("client_id")
        )
        route = "business" if intent in BUSINESS_INTENTS else "knowledge"
        return {"intent": intent.value, "route": route}

    def route_after_intent(state: LLAAgentState) -> Literal["identity_gate", "prepare_memory"]:
        return "identity_gate" if state.get("route") == "business" else "prepare_memory"

    def identity_gate(state: LLAAgentState) -> dict[str, Any]:
        if is_qa_whatsapp(state) and state.get("client_id") is None:
            return {
                "route": "blocked",
                "response_text": (
                    "Para consultar un perfil en esta demo, dime tu ID de cliente. "
                    "Puedes probar 1234, 5678 o 9012. Esta asociación solo sirve para QA."
                ),
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
        response = collections.negotiation_response(
            str(state.get("input_text", "")),
            str(state["client_id"]),
            _balance_from_state(state),
            Intent(str(state["intent"])),
            _tool_calls(state),
        )
        return {"response_text": response.text, "response_meta": _response_metadata(response)}

    def confirm_promise(state: LLAAgentState) -> dict[str, Any]:
        response = collections.confirm_promise_response(
            str(state["client_id"]), str(state.get("input_text", ""))
        )
        return {"response_text": response.text, "response_meta": _response_metadata(response)}

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
        if not safe_messages:
            update: dict[str, Any] = {
                "messages": [RemoveMessage(id=message.id) for message in older_messages]
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
        removals = [RemoveMessage(id=message.id) for message in older_messages]
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
