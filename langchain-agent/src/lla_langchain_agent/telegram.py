"""Optional Telegram runner that owns one runtime for the polling process."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from lla_agent.channels.telegram import build_application
from lla_agent.persistence.sqlite import SQLiteRepository

from .agent import DEFAULT_MODEL
from .models import chat_credentials_available
from .runtime import open_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = Path(__file__).resolve().parents[2]
load_dotenv(APP_DIR / ".env")


async def run() -> None:
    if not chat_credentials_available():
        raise RuntimeError(
            f"Configura GROQ_API_KEY en {APP_DIR / '.env'} o en el entorno."
        )
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("Define TELEGRAM_BOT_TOKEN fuera del código fuente.")

    database_path = Path(
        os.getenv("LLA_DB_PATH", str(PROJECT_ROOT / "data" / "collections.sqlite3"))
    )
    checkpoint_path = APP_DIR / "checkpoints.sqlite"
    repository = SQLiteRepository(database_path)
    async with open_runtime(
        repository=repository,
        checkpoint_path=checkpoint_path,
        model_name=os.getenv("GROQ_MODEL", DEFAULT_MODEL),
    ) as runtime:
        application = build_application(runtime)
        await application.initialize()
        await application.start()
        if application.updater is None:
            raise RuntimeError("La aplicación Telegram no tiene updater para polling.")
        await application.updater.start_polling()
        print("Agente Telegram listo. Presiona Ctrl+C para detenerlo.")
        try:
            await asyncio.Event().wait()
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nAgente Telegram detenido.")


if __name__ == "__main__":
    main()
