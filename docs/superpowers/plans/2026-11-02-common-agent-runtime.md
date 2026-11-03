# Common Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run local CLI, Chainlit, Telegram, and WhatsApp adapter messages through one LangGraph runtime so local behavior matches the future WhatsApp integration.

**Architecture:** Add a normalized inbound event and async `AgentRuntime` that owns one compiled graph and a lifecycle-managed SQLite checkpointer. Pass customer identity and shared local stores per process/turn rather than capturing a customer in graph closures. Keep LangGraph as the sole message-flow router, retain deterministic business operations, and compact old checkpoint history while preserving the audit log.

**Tech Stack:** Python 3.11, LangGraph 1.x, LangChain 1.x, `AsyncSqliteSaver`, SQLite, Groq, Chroma, Qdrant, Chainlit, optional python-telegram-bot.

**Spec:** `docs/superpowers/specs/2026-11-02-common-agent-runtime-design.md`

## Global Constraints

- CLI, Chainlit, Telegram, and the local WhatsApp adapter invoke the same `AgentRuntime` and compiled graph.
- The WhatsApp adapter receives an already normalized/verified mock event; no Meta webhook, send API, or AWS deployment is added.
- Customer IDs are per-invocation trusted context and must not be captured in graph/tool closures.
- The graph is the only message-flow router; domain operations remain deterministic and outside the LLM.
- Balance remains authoritative in SQLite; Qdrant/Chroma cannot authorize financial actions.
- Qdrant memory stays scoped by `client_id` and optional; Chroma is one shared public-web corpus for this local demo.
- Conversation thread IDs are stable and must not contain phone numbers or other raw external IDs.
- Do not add or run tests in this task; use static syntax/import/whitespace checks. Existing scenario tests may be adapted to the runtime contract but are not executed.
- Work in the current checkout; this workspace contains the active prototype as untracked source and a fresh worktree would omit it.

## Review Focus

- A runtime compiled for one customer must not expose that customer's context/tool result to another invocation.
- A changed customer or conversation must produce a distinct stable thread ID without exposing a raw phone number.
- A promise command with a different amount/date or extra text must not call `PaymentsTool`.
- A Qdrant connection/retrieval failure must leave the knowledge route available with empty memory context.
- History compaction must retain complete recent turns (including tool-call/result pairs) and summary while the full audit log remains in SQLite.

---

### Task 1: Add the normalized event and process-scoped runtime

**Files:**
- Modify: `src/lla_agent/domain/models.py`
- Create: `src/lla_agent/application/ports.py`
- Create: `langchain-agent/src/lla_langchain_agent/runtime.py`

**Interfaces:**
- `InboundMessage` is an immutable dataclass with `text: str`, `channel: str`, `message_id: str`, `external_id: str | None`, `client_id: str | None`, and `conversation_id: str`.
- `ConversationRuntime` is a protocol with `async handle(message: InboundMessage) -> AgentResponse`.
- `AgentRuntime` implements `handle(...)`, invokes the compiled graph with the normalized fields, and converts final graph state to `AgentResponse`.
- `open_runtime(...)` is an async context manager that opens one `AsyncSqliteSaver`, compiles the graph once, yields one `AgentRuntime`, then closes the saver.
- `thread_id` is a stable SHA-256 digest of channel, conversation ID, and client ID; raw external IDs are never embedded in checkpoint keys.

- [x] **Step 1: Define `InboundMessage` and `ConversationRuntime`** with the exact fields and method above; keep the base package free of LangGraph imports.
- [x] **Step 2: Implement `AgentRuntime.handle` and `open_runtime`** in `runtime.py`; own one compiled graph and checkpointer for the context-manager lifetime, and set a separate thread ID for each channel/conversation/customer.
- [x] **Step 3: Check runtime module syntax/importability** with `uv run --project langchain-agent python -m compileall -q src/lla_agent langchain-agent/src/lla_langchain_agent` and a direct import of `AgentRuntime`/`open_runtime`.

### Task 2: Make graph tools invocation-scoped and bound checkpoint history

**Files:**
- Modify: `langchain-agent/src/lla_langchain_agent/state.py`
- Modify: `langchain-agent/src/lla_langchain_agent/workflow.py`
- Modify: `langchain-agent/src/lla_langchain_agent/agent.py`
- Modify: `langchain-agent/src/lla_langchain_agent/rag.py`

**Interfaces:**
- `build_workflow(...)` does not accept fixed `client_id` or `external_id`; it receives process-scoped repository, model, checkpointer, `WebCorpus`, and optional Qdrant memory.
- `LLAAgentState` carries per-turn customer/channel/event context and `summary` for bounded model history.
- Customer tools read the validated client from LangChain tool runtime state; no customer value is captured when building the agent.
- `WebCorpus.search(query)` returns public page matches or an empty list when the shared corpus is empty/unavailable; `WebCorpus.replace(chunks, embeddings)` replaces the persisted Chroma collection and updates the same process-scoped object used by the graph.

- [x] **Step 1: Remove compile-time customer identity from `build_workflow`/`build_agent`** and use the supported LangChain tool runtime/state injection for scoped customer reads and dynamic prompt context.
- [x] **Step 2: Add a process-scoped `WebCorpus` facade** around the existing Chroma path with `search(query)` and `replace(chunks, embeddings)`; have the graph tool always call this facade so the already-compiled graph observes later local ingestion while Chainlit keeps showing its extraction/chunking/vectorization steps.
- [x] **Step 3: Add `summary` to state and a compaction route** that summarizes history after 20 messages, trims only at a complete human-turn boundary while retaining at least the latest 8 messages and all tool-call/result pairs, and keeps full message audit rows in `SQLiteRepository`.
- [x] **Step 4: Check changed graph modules with compileall/import checks**; do not invoke Groq, Qdrant, or the test suite.

### Task 3: Route local interfaces and adapters through the runtime

**Files:**
- Modify: `langchain-agent/src/lla_langchain_agent/cli.py`
- Modify: `langchain-agent/src/lla_langchain_agent/chainlit_app.py`
- Modify: `langchain-agent/pyproject.toml`
- Modify: `src/lla_agent/channels/whatsapp.py`
- Modify: `src/lla_agent/channels/telegram.py`
- Modify: `src/lla_agent/application/agent.py`
- Modify: `demo/run_demo.py`
- Modify: `tests/test_agent.py`
- Create: `langchain-agent/src/lla_langchain_agent/telegram.py`

**Interfaces:**
- CLI and Chainlit call `AgentRuntime.handle(InboundMessage)`; neither creates a graph per turn.
- `WhatsAppAdapter(runtime: ConversationRuntime)` exposes async `handle_verified_message(message, client_id=None) -> AgentResponse`, constructs the normalized event, and makes no network requests.
- The Telegram adapter receives the same runtime by injection; its runner opens the runtime once and manages polling within the runtime lifetime.
- `CollectionsAgent` exposes deterministic collection operations only; its monolithic `handle_message` router is removed when all consumers use the runtime.

- [x] **Step 1: Change CLI startup to open one runtime around its input loop** and pass each line as an `InboundMessage` with a stable conversation ID.
- [x] **Step 2: Change Chainlit to keep one process-level runtime/checkpointer** and send per-session/customer context on each message; preserve `/cliente`, web ingestion, retrieval display, and tool outcomes.
- [x] **Step 3: Inject `ConversationRuntime` into WhatsApp and Telegram adapters**; never infer identity from message text. Add an optional Telegram dependency/runner that opens the same runtime once.
- [x] **Step 4: Remove the second high-level dispatcher from `CollectionsAgent`**; migrate the local scenario demo and existing scenario-test inputs to the runtime contract without adding scenarios.
- [x] **Step 5: Update local run commands and statically compile/import changed application modules**; do not execute the migrated test suite.

### Task 4: Document the local runtime and WhatsApp boundary

**Files:**
- Modify: `README.md`
- Modify: `langchain-agent/README.md`
- Modify: `docs/langgraph-workflow-design.md`
- Modify: `docs/assessment-compliance.md`

- [x] **Step 1: Document runtime lifecycle and local commands** for CLI, Chainlit, Telegram, and local WhatsApp adapter; describe shared graph and hashed thread isolation.
- [x] **Step 2: Document history compaction and Qdrant client isolation**; state that pages are shared public context and never a billing source.
- [x] **Step 3: Document the real WhatsApp boundary**: Meta signature verification, webhook deduplication, public HTTPS, outbound API, and AWS deployment remain later work.
- [x] **Step 4: Run `git diff --check` and a whitespace scan over changed/untracked source/docs**; report static checks only and state that the test suite was not run.
