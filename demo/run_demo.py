"""Run the customer scenarios through the same local runtime used by the channels."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from lla_langchain_agent.runtime import open_runtime

from lla_agent.domain.models import AgentResponse, InboundMessage
from lla_agent.persistence.sqlite import SQLiteRepository


async def show(
    runtime,
    title: str,
    text: str,
    client_id: str | None,
    conversation_id: str,
) -> AgentResponse:
    response = await runtime.handle(
        InboundMessage(
            text=text,
            channel="demo",
            message_id=uuid4().hex,
            external_id=f"demo:{client_id}" if client_id else None,
            client_id=client_id,
            conversation_id=conversation_id,
        )
    )
    print(f"\n[{title}]\nUsuario: {text}\nAgente: {response.text}")
    print(
        "Tools:",
        ", ".join(call.name + ":" + call.outcome for call in response.tool_calls) or "ninguna",
    )
    return response


async def run_demo() -> None:
    repository = SQLiteRepository(":memory:")
    offline_model = FakeListChatModel(responses=["Esta respuesta simulada no se usa en estos escenarios."])
    async with open_runtime(
        repository=repository,
        checkpoint_path=":memory:",
        model=offline_model,
    ) as runtime:
        pending = await show(
            runtime,
            "Consulta y negociación",
            "Quiero pagar todo en 14 días",
            "C001",
            "promise-c001",
        )
        confirmation_command = pending.text.split("`")[1]
        await show(
            runtime,
            "Confirmación explícita",
            confirmation_command,
            "C001",
            "promise-c001",
        )
        await show(runtime, "Saldo disputado", "No reconozco este saldo", "C003", "dispute-c003")
        await show(
            runtime,
            "Descuento no autorizado",
            "Necesito una rebaja o descuento",
            "C002",
            "discount-c002",
        )
        await show(
            runtime,
            "Escalación humana",
            "Quiero hablar con un asesor",
            "C002",
            "human-c002",
        )
        await show(runtime, "Cliente desconocido", "¿Cuál es mi saldo?", "C404", "unknown-c404")
        await show(runtime, "Error de herramienta", "¿Cuál es mi saldo?", "CERR", "billing-cerr")
        await show(runtime, "Opt-out", "STOP", "C002", "optout-c002")
        await show(
            runtime,
            "Opt-out respetado",
            "¿Cuál es mi saldo?",
            "C002",
            "optout-c002",
        )


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
