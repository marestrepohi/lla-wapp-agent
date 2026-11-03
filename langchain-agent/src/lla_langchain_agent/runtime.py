"""Process-scoped runtime for all interactive LLA agent channels."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from lla_agent.domain.models import AgentResponse, InboundMessage, Intent, ToolCall
from lla_agent.persistence.sqlite import SQLiteRepository

from .agent import DEFAULT_MODEL
from .rag import WebCorpus
from .workflow import build_workflow

_LOGGER = logging.getLogger(__name__)


class AgentRuntime:
    """Invoke one compiled graph with per-message trusted context."""

    def __init__(self, graph: Any, web_corpus: WebCorpus) -> None:
        self._graph = graph
        self.web_corpus = web_corpus

    async def handle(self, message: InboundMessage) -> AgentResponse:
        """Run the graph for one inbound message and convert its final state."""
        result = await self._graph.ainvoke(
            {
                "messages": [HumanMessage(content=message.text, id=message.message_id)],
                "channel": message.channel,
                "message_id": message.message_id,
                "external_id": message.external_id,
                "client_id": message.client_id,
            },
            config={"configurable": {"thread_id": self.thread_id(message)}},
        )
        return self._response(result, message.message_id)

    @staticmethod
    def thread_id(message: InboundMessage) -> str:
        """Return a stable, non-PII checkpoint key for a conversation/customer pair."""
        raw_key = "\0".join(
            (
                message.channel,
                message.conversation_id,
                message.client_id or "anonymous",
            )
        )
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return f"{message.channel}:{digest}"

    @staticmethod
    def _response(result: dict[str, Any], message_id: str) -> AgentResponse:
        meta = result.get("response_meta", {})
        try:
            intent = Intent(str(meta.get("intent", result.get("intent", Intent.UNKNOWN.value))))
        except ValueError:
            intent = Intent.UNKNOWN

        calls = [
            ToolCall(
                name=str(item.get("name", "")),
                arguments=dict(item.get("arguments", {})),
                outcome=str(item.get("outcome", "unknown")),
            )
            for item in meta.get("tool_calls", [])
            if isinstance(item, dict)
        ]
        retrieved_documents: list[dict[str, object]] = []
        messages = result.get("messages", [])
        current_turn_start = max(
            (
                index
                for index, item in enumerate(messages)
                if isinstance(item, HumanMessage) and item.id == message_id
            ),
            default=-1,
        )
        for item in messages[current_turn_start + 1 :]:
            if not isinstance(item, ToolMessage):
                continue
            tool_name = str(item.name or "tool")
            outcome = "success"
            arguments: dict[str, object] = {}
            if tool_name == "buscar_en_paginas":
                try:
                    payload = json.loads(item.content) if isinstance(item.content, str) else {}
                except json.JSONDecodeError:
                    payload = {}
                if isinstance(payload, dict):
                    outcome = f"error: {payload['error']}" if payload.get("error") else outcome
                    matches = payload.get("matches", [])
                    if isinstance(matches, list):
                        retrieved_documents.extend(
                            match for match in matches if isinstance(match, dict)
                        )
            if not any(call.name == tool_name for call in calls):
                calls.append(ToolCall(tool_name, arguments, outcome))
        return AgentResponse(
            text=str(result.get("response_text", "No pude generar una respuesta.")),
            intent=intent,
            tool_calls=calls,
            escalated=bool(meta.get("escalated", result.get("escalated", False))),
            opted_out=bool(meta.get("opted_out", result.get("opted_out", False))),
            identity_validated=bool(
                meta.get("identity_validated", result.get("identity_validated", False))
            ),
            retrieved_documents=retrieved_documents,
        )


@asynccontextmanager
async def open_runtime(
    *,
    repository: SQLiteRepository,
    checkpoint_path: str | Path,
    model_name: str = DEFAULT_MODEL,
    model: BaseChatModel | None = None,
    web_corpus: WebCorpus | None = None,
    qa_id_linking: bool = False,
) -> AsyncIterator[AgentRuntime]:
    """Open one SQLite checkpointer and compile one graph for the runtime lifetime."""
    path = str(checkpoint_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    web_corpus = web_corpus or WebCorpus()
    async with AsyncSqliteSaver.from_conn_string(path) as checkpointer:
        graph = build_workflow(
            repository=repository,
            checkpointer=checkpointer,
            model_name=model_name,
            model=model,
            web_corpus=web_corpus,
            qa_id_linking=qa_id_linking,
        )
        _LOGGER.info("LLA graph runtime ready")
        yield AgentRuntime(graph, web_corpus)
