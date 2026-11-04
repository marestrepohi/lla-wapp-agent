"""Chainlit interface for the visible web-ingestion and customer-context RAG demo."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path

import chainlit as cl
from dotenv import load_dotenv

from lla_agent.domain.models import InboundMessage
from lla_agent.persistence.sqlite import SQLiteRepository

from .agent import DEFAULT_MODEL
from .rag import (
    DEFAULT_EMBEDDING_MODEL,
    WebCorpus,
    get_embeddings,
    load_web_pages,
    split_documents,
)
from .runtime import AgentRuntime, open_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = Path(__file__).resolve().parents[2]
load_dotenv(APP_DIR / ".env")

STATE_DIR = Path(os.getenv("LLA_STATE_DIR", str(APP_DIR)))
VECTOR_STORE_PATH = Path(os.getenv("LLA_VECTOR_STORE_PATH", str(STATE_DIR / "chroma")))
CHECKPOINT_PATH = Path(
    os.getenv("LLA_CHAINLIT_CHECKPOINT_PATH", str(STATE_DIR / "checkpoints.sqlite"))
)
DATABASE_PATH = Path(os.getenv("LLA_DB_PATH", str(PROJECT_ROOT / "data" / "collections.sqlite3")))
MODEL_NAME = os.getenv("GROQ_MODEL", DEFAULT_MODEL)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
QA_ID_LINKING = os.getenv("WHATSAPP_QA_ID_LINKING", "false").strip().casefold() in {
    "1", "true", "yes", "on"
}
_repository: SQLiteRepository | None = None
_web_corpus = WebCorpus(VECTOR_STORE_PATH, EMBEDDING_MODEL)
_runtime_context = None
_agent_runtime: AgentRuntime | None = None


def _runtime() -> AgentRuntime:
    if _agent_runtime is None:
        raise RuntimeError("El runtime Chainlit aún no inició.")
    return _agent_runtime


def _repo() -> SQLiteRepository:
    if _repository is None:
        raise RuntimeError("El repositorio Chainlit aún no inició.")
    return _repository


@cl.on_app_startup
async def on_app_startup() -> None:
    global _agent_runtime, _repository, _runtime_context
    _repository = SQLiteRepository(DATABASE_PATH)
    _runtime_context = open_runtime(
        repository=_repository,
        checkpoint_path=CHECKPOINT_PATH,
        model_name=MODEL_NAME,
        web_corpus=_web_corpus,
        qa_id_linking=QA_ID_LINKING,
    )
    _agent_runtime = await _runtime_context.__aenter__()


@cl.on_app_shutdown
async def on_app_shutdown() -> None:
    global _agent_runtime, _repository, _runtime_context
    if _runtime_context is not None:
        await _runtime_context.__aexit__(None, None, None)
    _agent_runtime = None
    _repository = None
    _runtime_context = None


def _extract_urls(text: str) -> list[str]:
    return [match.rstrip(".,)") for match in re.findall(r"https://[^\s,]+", text)]


def _retrieval_markdown(result: dict[str, object]) -> str:
    matches = result.get("matches", [])
    if not isinstance(matches, list) or not matches:
        return "No se encontraron fragmentos relevantes."

    sections = []
    for index, item in enumerate(matches, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "Página web"))
        source = str(item.get("source", ""))
        chunk_index = item.get("chunk_index", "?")
        distance = item.get("distance", "?")
        content = str(item.get("content", "")).replace("\n", " ")
        sections.append(
            f"**{index}. [{title}]({source})** · fragmento {chunk_index} · distancia `{distance}`\n\n"
            f"> {content[:700]}"
        )
    return "\n\n".join(sections) or "No se encontraron fragmentos relevantes."


async def _index_urls(urls: list[str], runtime: AgentRuntime) -> None:
    await cl.Message(content=f"Voy a procesar {len(urls)} página(s) públicas.").send()

    async with cl.Step(name="1 · Extracción de páginas", type="run") as step:
        step.input = "\n".join(urls)
        documents = await asyncio.to_thread(load_web_pages, urls)
        page_summary = [
            f"- **{doc.metadata['title']}**: {len(doc.page_content):,} caracteres · {doc.metadata['source']}"
            for doc in documents
        ]
        step.output = f"Se extrajeron {len(documents)} página(s).\n\n" + "\n".join(page_summary)

    async with cl.Step(name="2 · Chunking", type="run") as step:
        step.input = f"chunk_size={1000}, chunk_overlap={200}"
        chunks = await asyncio.to_thread(split_documents, documents)
        preview = "\n\n".join(
            f"**Fragmento {chunk.metadata['chunk_index']} · inicio {chunk.metadata.get('start_index', 0)}**\n"
            f"{chunk.page_content[:380].replace(chr(10), ' ')}…"
            for chunk in chunks[:3]
        )
        average_size = sum(len(chunk.page_content) for chunk in chunks) // max(len(chunks), 1)
        step.output = (
            f"{len(chunks)} fragmento(s), tamaño medio {average_size:,} caracteres.\n\n"
            f"Muestra:\n\n{preview}"
        )

    async with cl.Step(name="3 · Embeddings / vectorización", type="run") as step:
        step.input = f"Modelo local ONNX: {EMBEDDING_MODEL}"
        embeddings = await asyncio.to_thread(get_embeddings, EMBEDDING_MODEL)
        sample_vector = await asyncio.to_thread(embeddings.embed_query, chunks[0].page_content)
        sample_values = ", ".join(f"{value:.4f}" for value in sample_vector[:8])
        step.output = (
            f"Cada fragmento se transforma en un vector de **{len(sample_vector)} dimensiones**. "
            f"Ejemplo (primeras 8 coordenadas): `[{sample_values}, …]`."
        )

    async with cl.Step(name="4 · Almacenamiento vectorial", type="run") as step:
        step.input = str(VECTOR_STORE_PATH)
        ids = await asyncio.to_thread(runtime.web_corpus.replace, chunks, embeddings)
        step.output = (
            f"Se guardaron {len(ids)} vectores en la colección `lla_web_rag_demo` de Chroma. "
            f"La base local está en `{VECTOR_STORE_PATH}`."
        )

    await cl.Message(
        content=(
            "Indexación lista. Ahora haz una pregunta sobre esas páginas. "
            "En cada respuesta mostraré los fragmentos recuperados y sus fuentes. "
            "Puedes escribir `/ingestar` seguido de otras URLs para reemplazar el índice."
        )
    ).send()


@cl.on_chat_start
async def on_chat_start() -> None:
    session_id = cl.user_session.get("id") or uuid.uuid4().hex
    session_id = str(session_id)
    cl.user_session.set("session_id", session_id)
    cl.user_session.set("conversation_id", session_id)
    cl.user_session.set("external_id", f"chainlit:{session_id}")
    cl.user_session.set("client_id", None)
    if _web_corpus.indexed:
        context_status = "El contexto web guardado ya está cargado. Puedes preguntarme directamente."
    else:
        context_status = (
            "Puedes conversar conmigo directamente. No hay páginas web cargadas en este momento; "
            "si quieres añadir contexto, pega una URL HTTPS o usa `/ingestar` (opcional)."
        )
    await cl.Message(
        content=(
            "## Agente LLA\n\n"
            f"{context_status}\n\n"
            "Puedes empezar a conversar directamente. Para probar una consulta de cuenta, "
            "escribe tu ID en el chat; no necesitas seleccionar un cliente. En QA se crearán "
            "datos ficticios si el ID no existe. El comando `/cliente C001` sigue disponible "
            "como atajo opcional. La ingesta de páginas también es opcional."
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    content = message.content.strip()

    if content.casefold().startswith("/cliente "):
        client_id = content.split(maxsplit=1)[1].strip()
        if _repo().get_client(client_id) is None:
            await cl.Message(content="No existe ese cliente en el SQLite mock.").send()
            return
        normalized_client_id = client_id.strip().upper()
        external_id = str(cl.user_session.get("external_id") or "")
        link_status = _repo().link_client_to_external_id(normalized_client_id, external_id)
        if link_status == "conflict":
            await cl.Message(
                content="Esta sesión ya está asociada a otro perfil de prueba y no se puede cambiar desde el chat."
            ).send()
            return
        cl.user_session.set("client_id", normalized_client_id)
        await cl.Message(
            content=(
                f"Quedó seleccionado el cliente mock `{normalized_client_id}` para esta conversación. "
                "Este comando simula el contexto de identidad del canal."
            )
        ).send()
        return

    if content.casefold().startswith("/ingestar") or content.startswith("https://"):
        urls = _extract_urls(content)
        if not urls:
            await cl.Message(
                content="Escribe `/ingestar https://dominio/pagina` con una o más URLs HTTPS públicas."
            ).send()
            return
        try:
            await _index_urls(urls, _runtime())
        except Exception as error:
            await cl.Message(content=f"No se pudo indexar: {type(error).__name__}: {error}").send()
        return

    client_id = cl.user_session.get("client_id")
    external_id = cl.user_session.get("external_id")
    session_id = str(cl.user_session.get("conversation_id") or uuid.uuid4().hex)

    try:
        response = await _runtime().handle(
            InboundMessage(
                text=content,
                channel="chainlit",
                message_id=str(message.id or uuid.uuid4().hex),
                external_id=external_id,
                client_id=client_id,
                conversation_id=session_id,
            )
        )
    except Exception as error:
        await cl.Message(content=f"Falló la consulta: {type(error).__name__}: {error}").send()
        return

    if response.retrieved_documents:
        async with cl.Step(name="5 · Recuperación semántica", type="retrieval") as step:
            step.input = content
            step.output = _retrieval_markdown({"matches": response.retrieved_documents})

    if response.tool_calls:
        call_lines = [
            f"- `{item.name}`: {item.outcome}"
            for item in response.tool_calls
        ]
        async with cl.Step(name="Flujo LangGraph", type="run") as step:
            step.input = content
            step.output = (
                f"Intención: `{response.intent.value}`\n\n"
                f"Identidad validada: `{response.identity_validated}` · "
                f"Escalado: `{response.escalated}` · "
                f"Opt-out: `{response.opted_out}`\n\nTools:\n" + "\n".join(call_lines)
            )

    await cl.Message(content=response.text).send()
