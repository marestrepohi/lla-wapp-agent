# LangGraph Collections Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Orchestrate the local customer-collections flow in an explicit LangGraph while preserving the existing business rules and tools.

**Architecture:** A parent `StateGraph` owns the conversation checkpoint and branches through policy, identity, collection actions, and knowledge/RAG nodes. The deterministic collections policy is exposed as reusable operations; Groq `create_agent` remains a knowledge subgraph, while customer financial operations stay behind scoped SQLite-backed tools.

**Tech Stack:** Python 3.11, LangGraph `StateGraph`, LangChain `create_agent`, Groq, SQLite checkpointer, Qdrant customer memory, Chroma page RAG, Chainlit.

**Spec:** `docs/langgraph-workflow-design.md`

## Global Constraints

- STOP/opt-out and identity checks must happen before collection data is read.
- Only trusted adapter/session context may set `client_id`; graph code must not infer identity from user text.
- SQLite billing data remains the authority for balance; Qdrant/Chroma are never financial sources.
- Promise writes require an existing pending proposal and explicit customer confirmation.
- Keep existing local deterministic demo behavior through `CollectionsAgent.handle_message`.
- Do not add or run tests in this implementation, per the higher-priority session instruction; perform static syntax and whitespace checks only.
- Work in the existing checkout because the repository work is present as untracked files and a linked worktree from `main` would omit it.

## Review Focus

- STOP from an unknown sender must persist opt-out before any model or billing call; inspect gate ordering and the graph route.
- A collection request without a trusted client ID must return an identity prompt without querying billing; inspect the identity conditional edge.
- A confirmation command must match the pending proposal amount and date; a short or stale confirmation must not invoke the payment write.
- A billing outage must return no amount and create a human escalation; inspect the read-balance error edge.
- Changing the selected mock client in Chainlit must select a different checkpoint thread; inspect the `/cliente` command and thread ID builder.

---

### Task 1: Expose reusable collection policy operations

**Files:**
- Modify: `src/lla_agent/application/agent.py`

**Interfaces:**
- Produce public turn operations for inbound/opt-out handling, client validation, intent classification, balance lookup, negotiation proposal, confirmation, escalation, and outbound persistence.
- Preserve the public `CollectionsAgent.handle_message(...) -> AgentResponse` behavior by composing those same operations.
- Resolve identity only from trusted channel context or the persisted external-ID mapping; never infer it from message text.

- [x] Extract shared operations from the current monolithic `handle_message` flow without changing its existing offline-demo response behavior.
- [x] Make side-effecting methods return response data separately from outbound logging so the graph persists one outbound response per turn.
- [x] Preserve explicit-confirmation and idempotent promise-write behavior.
- [x] Inspect current tests to ensure their public API assumptions remain represented; do not run them.

### Task 2: Add explicit LangGraph state and workflow

**Files:**
- Create: `langchain-agent/src/lla_langchain_agent/state.py`
- Create: `langchain-agent/src/lla_langchain_agent/workflow.py`
- Modify: `docs/langgraph-workflow-design.md`

**Interfaces:**
- `LLAAgentState` extends LangChain `AgentState` with trusted channel/customer context, intent, balance fields, memory context, response metadata, and tool outcomes.
- `build_workflow(...)` accepts repository, root checkpointer, model, optional vector store, and optional customer memory; it returns a compiled graph.
- Nodes: intake/opt-out, intent routing, identity gate, balance read, negotiation proposal, promise confirmation, escalation, memory preparation, knowledge agent, and final response persistence.

- [x] Keep opt-out as the first gate. Route public/general questions to knowledge/RAG even when no customer is selected; require identity only for collection/account routes. This preserves the existing direct-chat demo behavior.
- [x] Add explicit identity and collection conditional routes; the graph never trusts a client ID found in message content.
- [x] Add balance, negotiation, confirmation, and escalation nodes that call the shared `CollectionsAgent` operations.
- [x] Keep Qdrant extraction/retrieval in a graph node after collection suppression; inject memory into the knowledge agent as untrusted context.
- [x] Compile the parent graph with the supplied checkpointer and the `create_agent` knowledge subgraph without a second independent checkpointer.

### Task 3: Integrate CLI and Chainlit with the root graph

**Files:**
- Modify: `langchain-agent/src/lla_langchain_agent/agent.py`
- Modify: `langchain-agent/src/lla_langchain_agent/cli.py`
- Modify: `langchain-agent/src/lla_langchain_agent/chainlit_app.py`

**Interfaces:**
- `build_agent(...)` returns a knowledge/RAG subgraph with the shared state schema and a dynamic prompt; it does not own conversation persistence.
- CLI and Chainlit invoke `build_workflow(...)` and pass a stable `thread_id` in graph config.

- [x] Remove external `prepare_memory_context` calls and Qdrant memory tools from the agent after graph memory nodes are wired.
- [x] Pass stable `client_id`, `external_id`, channel, and message ID as trusted graph input.
- [x] Scope Chainlit thread IDs by session and selected customer; update the thread when `/cliente` changes the mock customer.
- [x] Preserve RAG step display by extracting retrieval tool results from graph messages.

### Task 4: Update docs and perform static verification

**Files:**
- Modify: `langchain-agent/README.md`
- Modify: `README.md`

- [x] Document the LangGraph flow, local run commands, customer scoping, and that real Meta webhook integration remains separate.
- [x] Run `uv run --project langchain-agent python -m compileall -q src/lla_agent langchain-agent/src/lla_langchain_agent`; exit code 0.
- [x] Run `git diff --check`; no whitespace errors.
- [x] Review the graph routes against all nine scenarios in the spec without executing the test suite.
