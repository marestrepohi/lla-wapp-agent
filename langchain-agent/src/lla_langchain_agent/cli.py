"""Interactive local chat using the configured shared agent runtime."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from lla_agent.domain.models import InboundMessage
from lla_agent.persistence.sqlite import SQLiteRepository

from .agent import DEFAULT_MODEL
from .memory import qdrant_memory_configured
from .models import chat_credentials_available
from .runtime import open_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = Path(__file__).resolve().parents[2]
load_dotenv(APP_DIR / ".env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat local con el agente LLA")
    parser.add_argument(
        "--client-id",
        required=True,
        help="ID del cliente mock validado para esta sesión; por ejemplo, C001",
    )
    parser.add_argument(
        "--thread-id",
        help="Sufijo opcional para separar conversaciones locales",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("GROQ_MODEL", DEFAULT_MODEL),
        help=f"Modelo Groq de respaldo (por defecto: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("LLA_DB_PATH", str(PROJECT_ROOT / "data" / "collections.sqlite3")),
        help="Ruta de la base mock del proyecto",
    )
    parser.add_argument(
        "--checkpoints-path",
        default=str(APP_DIR / "checkpoints.sqlite"),
        help="Archivo SQLite para memoria de conversación",
    )
    return parser.parse_args()


async def run_chat(args: argparse.Namespace) -> None:
    if not chat_credentials_available():
        raise SystemExit(
            f"Configura GROQ_API_KEY en {APP_DIR / '.env'} o en el entorno."
        )

    database_path = Path(args.db_path)
    if str(database_path) != ":memory:":
        database_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = Path(args.checkpoints_path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(database_path)
    conversation_id = f"{args.thread_id or 'default'}"

    async with open_runtime(
        repository=repository,
        checkpoint_path=checkpoint_path,
        model_name=args.model,
    ) as runtime:
        print(f"Agente listo para el cliente mock {args.client_id}. Escribe 'salir' para terminar.")
        if qdrant_memory_configured():
            print("Memoria Qdrant: se cargará de forma diferida en preguntas generales del cliente.")
        else:
            print("Memoria a largo plazo Qdrant: desactivada; configura QDRANT_URL y QDRANT_API_KEY.")

        while True:
            try:
                text = await asyncio.to_thread(input, "Tú: ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            text = text.strip()
            if text.casefold() in {"salir", "exit", "quit"}:
                break
            if not text:
                continue

            response = await runtime.handle(
                InboundMessage(
                    text=text,
                    channel="local",
                    message_id=uuid4().hex,
                    external_id=f"local:{args.client_id}",
                    client_id=args.client_id,
                    conversation_id=conversation_id,
                )
            )
            print(f"Agente: {response.text}")
            tool_names = [call.name for call in response.tool_calls]
            if tool_names:
                print(f"Flujo: {response.intent.value} · tools: {', '.join(tool_names)}")


def main() -> None:
    asyncio.run(run_chat(parse_args()))


if __name__ == "__main__":
    main()
