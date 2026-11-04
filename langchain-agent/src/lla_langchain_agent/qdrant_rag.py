"""Remote Qdrant collection for public website retrieval in AWS workers."""

from __future__ import annotations

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

from .embeddings import DEFAULT_EMBEDDING_MODEL, FastEmbedEmbeddings

DEFAULT_WEB_COLLECTION = "lla_public_web_pages_fastembed_v1"


class QdrantWebCorpus:
    """Remote vector corpus with the same search/replace surface as local Chroma."""

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        collection_name: str = DEFAULT_WEB_COLLECTION,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        client: QdrantClient | None = None,
        embedding: Embeddings | None = None,
    ) -> None:
        if not url.strip() or not api_key.strip():
            raise ValueError("QDRANT_URL y la clave de Qdrant son obligatorios.")
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.client = client or QdrantClient(
            url=url,
            api_key=api_key,
            prefer_grpc=False,
            timeout=10,
        )
        self.embedding = embedding or FastEmbedEmbeddings(embedding_model)
        self._vector_store: QdrantVectorStore | None = None
        if self.client.collection_exists(collection_name):
            self._vector_store = QdrantVectorStore(
                client=self.client,
                collection_name=collection_name,
                embedding=self.embedding,
            )

    @property
    def indexed(self) -> bool:
        if self._vector_store is None:
            return False
        return bool(self.client.count(collection_name=self.collection_name).count)

    def replace(self, chunks: list[Document], embeddings: Embeddings | None = None) -> list[str]:
        """Replace the public corpus collection with an explicit ingestion run."""
        if not chunks:
            raise ValueError("No hay fragmentos para indexar.")
        selected_embeddings = embeddings or self.embedding
        if self.client.collection_exists(self.collection_name):
            self.client.delete_collection(self.collection_name)
        dimensions = len(selected_embeddings.embed_query(chunks[0].page_content))
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.COSINE),
        )
        vector_store = QdrantVectorStore(
            client=self.client,
            collection_name=self.collection_name,
            embedding=selected_embeddings,
        )
        ids = vector_store.add_documents(chunks)
        self.embedding = selected_embeddings
        self._vector_store = vector_store
        return ids

    def search(self, query: str, *, limit: int = 3) -> list[tuple[Document, float]]:
        """Return documents with a distance-like value (lower means closer)."""
        if self._vector_store is None:
            return []
        matches = self._vector_store.similarity_search_with_score(query, k=limit)
        return [(document, 1.0 - float(score)) for document, score in matches]


def connect_qdrant_web_corpus(
    *,
    url: str,
    api_key: str,
    collection_name: str = DEFAULT_WEB_COLLECTION,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> QdrantWebCorpus:
    return QdrantWebCorpus(
        url=url,
        api_key=api_key,
        collection_name=collection_name,
        embedding_model=embedding_model,
    )
