# Natural WhatsApp Opening and Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Carry the approved WhatsApp template context into LangGraph and guide the recipient through contact confirmation, customer ID linking, and an immediate SQLite-backed case summary.

**Architecture:** Meta status callbacks correlate a template's `biz_opaque_callback_data` with the recipient in SQLite. The first inbound WhatsApp message consumes the context and starts a persistent LangGraph stage; after confirmation and a QA ID link, the existing deterministic collection service returns the account summary. The shared model factory uses the configured Groq model for conversational inference and Groq for media processing.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, LangChain `ChatGroq`, LangGraph SQLite checkpointer, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-03-whatsapp-opening-context-design.md`

## Global Constraints

- SQLite remains authoritative for customer IDs, invoices, and balances.
- QA ID linking is not real identity verification and remains controlled by `WHATSAPP_QA_ID_LINKING`.
- Callback data is strictly parsed, size limited, and treated as untrusted context; it never contains API credentials.
- Simulated account data is disclosed once when its initial balance is presented; later replies do not repeat the disclosure.
- `GROQ_API_KEY` powers conversational inference and WhatsApp audio/image understanding.
- Do not add or run tests; verify through compile/import, dependency lock validation, and diff inspection.

## Review Focus

- A recipient answers `no`: the thread must not reveal account data or continue collection messages.
- A recipient answers `yes`, then sends a bare ID: the ID must be accepted only while the graph is waiting for it.
- A newly generated QA ID must persist and yield invoice-derived totals; a billing error must not yield guessed amounts.
- A repeated Meta status callback must not restore an already consumed opening context.
- Missing `GROQ_API_KEY` must fail fast with a clear setup message.

---

### Task 1: Shared Groq model configuration

**Files:**
- Create: `langchain-agent/src/lla_langchain_agent/models.py`
- Modify: `langchain-agent/src/lla_langchain_agent/agent.py`
- Modify: `langchain-agent/src/lla_langchain_agent/workflow.py`
- Modify: `langchain-agent/src/lla_langchain_agent/memory.py`
- Modify: `langchain-agent/src/lla_langchain_agent/cli.py`
- Modify: `langchain-agent/src/lla_langchain_agent/telegram.py`
- Modify: `langchain-agent/src/lla_langchain_agent/whatsapp_webhook.py`
- Modify: `langchain-agent/src/lla_langchain_agent/chainlit_app.py`
- Modify: `langchain-agent/pyproject.toml`, `langchain-agent/uv.lock`, `langchain-agent/.env.example`

**Interfaces:**
- Produces: `build_chat_model(groq_model: str) -> BaseChatModel` and `chat_credentials_available() -> bool`.
- Produces: Groq model selection through `GROQ_MODEL` and a single `GROQ_API_KEY` requirement.
- `prepare_memory_context(..., model: BaseChatModel | None = None)` may reuse the graph's configured model for safe memory extraction.

- [x] Keep `langchain-groq` as the only chat-provider dependency and regenerate `uv.lock`.
- [x] Build the configured Groq chat model and fail fast if `GROQ_API_KEY` is missing.
- [x] Route workflow, CLI, Chainlit, Telegram, and webhook startup through the shared credential/model selection logic without changing Groq media processing.
- [x] Reuse the selected model for memory extraction rather than constructing a Groq-only analyzer.

### Task 2: Persist and consume template opening context

**Files:**
- Modify: `src/lla_agent/persistence/sqlite.py`
- Create: `langchain-agent/src/lla_langchain_agent/whatsapp_opening.py`
- Modify: `src/lla_agent/domain/models.py`
- Modify: `src/lla_agent/channels/whatsapp.py`
- Modify: `langchain-agent/src/lla_langchain_agent/whatsapp_webhook.py`
- Modify: `langchain-agent/src/lla_langchain_agent/runtime.py`
- Modify: `langchain-agent/src/lla_langchain_agent/state.py`

**Interfaces:**
- Produces: `parse_opening_callback_data(value: str) -> dict[str, str] | None` for an allowlisted compact context.
- Produces: SQLite `record_message_status(..., recipient_id=None, callback_data=None)` and `consume_whatsapp_opening_context(external_id) -> dict[str, str] | None`.
- Produces: optional `opening_context` on `InboundMessage` and WhatsApp adapter calls.

- [x] Persist callback context by WAMID and WhatsApp sender from status events; preserve existing delivery-status ordering and idempotency.
- [x] Consume the newest pending context once when the first inbound message is processed; repeated status webhooks must not reset consumption.
- [x] Pass allowlisted context through the WhatsApp adapter and runtime into persistent LangGraph state.
- [x] Keep malformed callback data, oversized values, unknown flow names, and control characters out of prompts.

### Task 3: Natural contact confirmation and proactive account overview

**Files:**
- Modify: `langchain-agent/src/lla_langchain_agent/state.py`
- Modify: `langchain-agent/src/lla_langchain_agent/workflow.py`
- Modify: `langchain-agent/src/lla_langchain_agent/agent.py`

**Interfaces:**
- LangGraph state gains `opening_stage` with `awaiting_contact_confirmation`, `awaiting_client_id`, `contact_mismatch`, and `complete` values.
- The workflow handles affirmative, negative, and unclear confirmation replies before intent classification.
- Successful QA ID linking returns a deterministic `CollectionsAgent.balance_response` based on SQLite records in that same turn.

- [x] Add a confirmation stage: yes asks for the client ID, no ends account collection on that thread, and an unclear answer briefly identifies LLA and repeats the confirmation question.
- [x] Accept an ID in the confirmation reply or as a bare message only while awaiting the ID; preserve the one-number/one-client conflict rule.
- [x] On successful linking, read SQLite balance/invoices immediately and present the case plus one next-step question; do not reveal data after a negative response.
- [x] Replace repeated QA/demo disclaimers in the prompt/finalizer with one concise disclosure on the first summary for a generated simulated profile.
- [x] Add the opening context to dynamic model instructions as untrusted background so later free-form replies continue the same thread naturally.

### Task 4: Curl usage and operational documentation

**Files:**
- Modify: `README.md`
- Modify: `langchain-agent/README.md`
- Modify: `scripts/send_whatsapp_template.py`
- Modify: `langchain-agent/.env.example`

- [x] Document a PowerShell and Bash Meta template request using environment variables for the token and a compact `biz_opaque_callback_data` value.
- [x] Explain that the visible message must match an approved Meta template, callback data only carries context, and the opening question should ask for contact confirmation.
- [x] Update local startup, Groq model configuration, and a concise expected WhatsApp conversation example.
- [x] Verify compilation/imports, dependency lock consistency, Docker Compose configuration parsing, and `git diff --check`; do not run tests.
