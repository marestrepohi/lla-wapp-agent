"""Shared state schema for the parent workflow and its LangChain agent subgraph."""

from __future__ import annotations

from typing import Any

from langchain.agents import AgentState


class LLAAgentState(AgentState, total=False):
    """Conversation messages plus trusted channel context and current-turn results."""

    client_id: str | None
    external_id: str | None
    channel: str
    message_id: str
    input_text: str
    intent: str
    route: str
    response_text: str
    response_meta: dict[str, Any]
    identity_validated: bool
    awaiting_client_id: bool
    opted_out: bool
    escalated: bool
    balance: dict[str, Any]
    billing_failed: bool
    tool_calls: list[dict[str, Any]]
    memory_context: str
    summary: str
