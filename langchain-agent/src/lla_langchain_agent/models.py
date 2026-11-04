"""Shared chat-model selection for local channels and the LangGraph runtime."""

from __future__ import annotations

import logging
import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_groq import ChatGroq

_LOGGER = logging.getLogger(__name__)


def chat_credentials_available() -> bool:
    """Return whether Groq is configured for local inference."""
    return bool(os.getenv("GROQ_API_KEY", "").strip())


def build_chat_model(groq_model: str) -> BaseChatModel:
    """Build the Groq model used by the local agent."""
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key:
        raise RuntimeError("No hay una clave GROQ_API_KEY configurada.")
    _LOGGER.info("Using Groq conversational model %s", groq_model)
    return ChatGroq(
        model=groq_model,
        api_key=groq_key,
        temperature=0,
        max_retries=1,
    )
