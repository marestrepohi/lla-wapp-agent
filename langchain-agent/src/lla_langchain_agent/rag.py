"""Web-page ingestion, chunking, local embeddings, and persistent vector search."""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .embeddings import DEFAULT_EMBEDDING_MODEL, FastEmbedEmbeddings

MAX_PAGES = 5
MAX_HTML_BYTES = 2_000_000
MAX_PAGE_CHARS = 100_000
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200
COLLECTION_NAME = "lla_web_rag_demo"
_LOGGER = logging.getLogger(__name__)


def _validate_public_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"Solo se aceptan URLs HTTPS públicas: {url}")
    if parsed.username or parsed.password:
        raise ValueError("La URL no puede incluir usuario ni contraseña.")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("No se permiten nombres de host locales.")

    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    host,
                    parsed.port or 443,
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as error:
            raise ValueError(f"No se pudo resolver el dominio {host}.") from error

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("La URL debe resolver únicamente a direcciones IP públicas.")


def _get_page(url: str) -> tuple[str, str, str]:
    current_url = url
    headers = {"User-Agent": "LLA-RAG-Demo/0.1 (local educational prototype)"}

    for _ in range(4):
        _validate_public_https_url(current_url)
        response = requests.get(
            current_url,
            headers=headers,
            timeout=20,
            allow_redirects=False,
            stream=True,
        )
        try:
            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError(f"La página respondió {response.status_code} sin destino de redirección.")
                current_url = urljoin(current_url, location)
                continue

            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                raise ValueError(f"La URL no entregó una página HTML: {current_url}")

            body = bytearray()
            for block in response.iter_content(chunk_size=64 * 1024):
                if not block:
                    continue
                if len(body) + len(block) > MAX_HTML_BYTES:
                    raise ValueError(f"La página supera el límite de {MAX_HTML_BYTES // 1_000_000} MB.")
                body.extend(block)
            html = bytes(body).decode(response.encoding or "utf-8", errors="replace")
            return html, current_url, content_type
        finally:
            response.close()

    raise ValueError(f"Demasiadas redirecciones al cargar {url}.")


def load_web_pages(urls: list[str]) -> list[Document]:
    """Fetch and clean a small set of public web pages into LangChain Documents."""
    unique_urls = list(dict.fromkeys(url.strip() for url in urls if url.strip()))
    if not unique_urls:
        raise ValueError("Agrega al menos una URL HTTPS.")
    if len(unique_urls) > MAX_PAGES:
        raise ValueError(f"La demo permite hasta {MAX_PAGES} páginas por indexación.")

    documents: list[Document] = []
    for url in unique_urls:
        html, final_url, _ = _get_page(url)
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else final_url

        for element in soup.find_all(
            ["script", "style", "nav", "footer", "header", "aside", "noscript", "svg"]
        ):
            element.decompose()

        content_node = soup.find(["main", "article"]) or soup.body or soup
        page_text = content_node.get_text("\n", strip=True)
        if not page_text:
            raise ValueError(f"No se pudo extraer texto legible de {final_url}.")

        documents.append(
            Document(
                page_content=page_text[:MAX_PAGE_CHARS],
                metadata={"source": final_url, "title": title},
            )
        )
    return documents


def split_documents(documents: list[Document]) -> list[Document]:
    """Split loaded pages while retaining their source and character offsets."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        add_start_index=True,
    )
    chunks = splitter.split_documents(documents)
    counters: defaultdict[str, int] = defaultdict(int)
    for chunk in chunks:
        source = str(chunk.metadata.get("source", ""))
        counters[source] += 1
        chunk.metadata["chunk_index"] = counters[source]
    return chunks


@lru_cache(maxsize=1)
def get_embeddings(model_name: str = DEFAULT_EMBEDDING_MODEL) -> FastEmbedEmbeddings:
    """Return a cached FastEmbed CPU embedding adapter; no API key is required."""
    return FastEmbedEmbeddings(model_name)


class LazyLocalEmbeddings(Embeddings):
    """Defer loading the local transformer until the first vector search."""

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL) -> None:
        self.model_name = model_name

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return get_embeddings(self.model_name).embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return get_embeddings(self.model_name).embed_query(text)


def index_documents(
    chunks: list[Document],
    embeddings: Embeddings,
    persist_directory: str,
) -> tuple[Chroma, list[str]]:
    """Replace this demo collection and persist the new vectors locally in Chroma."""
    vector_store = Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
        persist_directory=persist_directory,
    )
    vector_store.reset_collection()
    ids = vector_store.add_documents(documents=chunks)
    return vector_store, ids


class WebCorpus:
    """Process-scoped facade over the shared public-page Chroma collection."""

    def __init__(
        self,
        persist_directory: str | Path | None = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self.persist_directory = Path(
            persist_directory or Path(__file__).resolve().parents[2] / "chroma"
        )
        self.embedding_model = embedding_model
        self._lock = threading.RLock()
        try:
            self._vector_store = load_existing_index(
                str(self.persist_directory), self.embedding_model
            )
        except Exception as error:
            _LOGGER.warning("Saved web corpus unavailable: %s", type(error).__name__)
            self._vector_store = None

    @property
    def indexed(self) -> bool:
        with self._lock:
            return self._vector_store is not None

    def replace(
        self,
        chunks: list[Document],
        embeddings: Embeddings,
    ) -> list[str]:
        """Replace the persisted collection and publish it to all runtime tools."""
        with self._lock:
            vector_store, ids = index_documents(
                chunks,
                embeddings,
                str(self.persist_directory),
            )
            self._vector_store = vector_store
            return ids

    def search(self, query: str, *, limit: int = 3) -> list[tuple[Document, float]]:
        """Search the shared corpus; return no matches before ingestion."""
        with self._lock:
            if self._vector_store is None:
                return []
            return self._vector_store.similarity_search_with_score(query, k=limit)


def load_existing_index(
    persist_directory: str,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> Chroma | None:
    """Open the saved web collection when it exists and contains at least one chunk."""
    if not (Path(persist_directory) / "chroma.sqlite3").is_file():
        return None

    vector_store = Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=LazyLocalEmbeddings(embedding_model),
        persist_directory=persist_directory,
    )
    if not vector_store.get(limit=1).get("ids"):
        return None
    return vector_store
