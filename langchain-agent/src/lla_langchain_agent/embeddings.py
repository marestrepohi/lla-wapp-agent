"""CPU ONNX embeddings shared by the local RAG and customer memory stores."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from langchain_core.embeddings import Embeddings

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@lru_cache(maxsize=2)
def _load_model(model_name: str, cache_dir: str | None) -> Any:
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model_name, cache_dir=cache_dir)


class FastEmbedEmbeddings(Embeddings):
    """LangChain adapter for FastEmbed's CPU ONNX implementation."""

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL) -> None:
        self.model_name = model_name
        self.cache_dir = os.getenv("FASTEMBED_CACHE_PATH") or None

    @property
    def _model(self) -> Any:
        return _load_model(self.model_name, self.cache_dir)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return next(self._model.query_embed(text)).tolist()
