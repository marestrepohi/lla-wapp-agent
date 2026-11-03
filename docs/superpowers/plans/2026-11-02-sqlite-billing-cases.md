# SQLite Billing Cases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three realistic, fictional, overdue mobile/internet/TV customer cases to SQLite and let the WhatsApp agent answer from those records while retaining its existing SQLite conversation memory.

**Architecture:** Extend the existing SQLite repository with normalized service, invoice, and collection-event tables. Make open invoice amounts the billing source for the new cases, expose account details through the shared agent, and leave LangGraph's persistent SQLite checkpointer responsible for per-WhatsApp conversation continuity.

**Tech Stack:** Python 3.11, SQLite, LangChain tools, LangGraph, FastAPI WhatsApp webhook.

**Spec:** `docs/superpowers/specs/2026-11-02-sqlite-billing-cases-design.md`

## Global Constraints

- All new customer, service, invoice, and collection-event data is synthetic.
- Seed cases for numeric client IDs `1234`, `5678`, and `9012` with mobile, home internet, and/or TV services.
- Keep SQLite as the authority for balances and invoice details.
- Keep the existing conversation checkpointer at `langchain-agent/whatsapp-checkpoints.sqlite` and the existing message log in `data/collections.sqlite3`.
- Keep `WHATSAPP_QA_ID_LINKING` opt-in; a typed mock ID does not verify a real person's identity.
- Do not add or run tests in this implementation; use static syntax, whitespace, and application-startup checks.

## Review Focus

- Partial invoice payments must reduce the open amount by `paid_cents` before totals are computed; inspect the SQL aggregation and result fields.
- Paid invoices must not contribute to the open balance; inspect the invoice query's status/amount conditions.
- Existing customers with legacy aggregate balances must keep working; inspect compatibility behavior for clients without invoice rows.
- Billing-unavailable clients must continue to fail closed without returning an invented balance; inspect `billing_available` handling.
- WhatsApp account detail queries must require the graph's linked `client_id`; inspect the tool's state gate.

---

### Task 1: Add normalized billing tables and idempotent QA fixtures

**Files:**
- Modify: `src/lla_agent/persistence/sqlite.py`

**Interfaces:**
- Add `client_services`, `invoices`, and `collection_events` tables with foreign keys and stable fixture IDs.
- Seed service and invoice rows for `1234`, `5678`, and `9012`; preserve existing clients and legacy fixture behavior.
- Seed synthetic collection events describing prior reminders, customer responses, and payment-promise outcomes.

- [x] Add table creation statements and indexes in `_initialize()` so existing databases migrate on next startup.
- [x] Add `INSERT OR IGNORE` fixture rows with stable IDs, fictional PAB amounts, and overdue dates; make repeated startup idempotent.
- [x] Ensure each numeric client has multiple plan/invoice rows and the three cases differ: `1234` mobile plus home internet, `5678` home internet plus TV with a missed promise, and `9012` mobile plus internet with a partial payment and recent collection event.

### Task 2: Query invoice totals and customer account details from SQLite

**Files:**
- Modify: `src/lla_agent/persistence/sqlite.py`
- Modify: `src/lla_agent/domain/models.py`

**Interfaces:**
- Keep `SQLiteRepository.get_balance(client_id: str) -> Balance` as the billing API.
- Add `SQLiteRepository.get_account_details(client_id: str) -> dict[str, Any] | None` returning client identity, service plans, invoice periods/due dates/amounts/paid/open amounts/statuses, and recent synthetic collection events.
- The open balance for fixture customers is the sum of `amount_cents - paid_cents` for unpaid or partially paid invoices; the balance due date is the oldest due date with an open amount.

- [x] Implement detail retrieval with parameterized SQL and deterministic ordering by service, due date, and event time.
- [x] Derive fixture `Balance` values from open invoice rows, including partial payments; preserve legacy aggregate behavior for existing clients without invoice rows.
- [x] Preserve the existing billing-unavailable behavior and currency.

### Task 3: Use SQL account details in agent responses

**Files:**
- Modify: `src/lla_agent/application/agent.py`
- Modify: `langchain-agent/src/lla_langchain_agent/agent.py`
- Modify: `langchain-agent/src/lla_langchain_agent/workflow.py` only if response state needs a new field.

**Interfaces:**
- `CollectionsAgent.balance_response(...)` continues to accept a `Balance` and includes the fixture's service/invoice breakdown from `get_account_details`.
- `consultar_contexto_cliente` returns JSON from `get_account_details` for an already linked `client_id`, including service plan, open invoices, and recent collection history.
- Agent instructions use SQL for all client-specific account, plan, invoice, and collection-history facts; conversation summaries remain non-authoritative for financial details.

- [x] Extend the deterministic balance response with a concise service and overdue-invoice breakdown.
- [x] Extend the existing customer-context tool description and structured output with SQL account details; preserve the no-linked-client response.
- [x] Update the system prompt so client-specific service and collection questions call the SQL tool.
- [x] Route informational questions about invoices and previous promises to the SQL-backed account-context tool before broad balance/negotiation keywords.

### Task 4: Document and activate the local SQLite dataset

**Files:**
- Modify: `langchain-agent/README.md`
- Modify: `docs/architecture.md` if it describes the current schema or memory split.

- [x] Document the three numeric fixtures, example WhatsApp interactions, how invoice totals are computed, and the separate roles of repository messages and the LangGraph SQLite checkpointer.
- [x] Run static Python compilation and `git diff --check`.
- [x] Restart the existing local WhatsApp webhook so repository initialization creates/migrates and seeds `data/collections.sqlite3`; confirm the health endpoint is available.

## Execution Notes

Tasks 1 and 2 establish the schema and repository contract. Task 3 consumes that contract. Task 4 updates docs and initializes the actual local database. Implement natively in this checkout; the tasks share SQLite interfaces and should be reviewed together.
