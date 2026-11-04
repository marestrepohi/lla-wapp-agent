# Stateful WhatsApp Payment Follow-Up Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make amount/date replies continue the same payment-plan flow without slow RAG or LLM routing.

**Architecture:** Keep payment-flow stage and partial amount in LangGraph's SQLite checkpoint. Parse
amounts and dates in the collections service, persist a complete proposal in the existing
`pending_promises` table, and require the current explicit confirmation command to register it.
Compact WhatsApp history without a synchronous LLM summary.

**Tech Stack:** Python, LangGraph, SQLite, Groq.

**Spec:** `docs/superpowers/specs/2026-11-04-stateful-payment-followup.md`

## Global Constraints

- SQLite is authoritative for balances, invoices, and promises.
- A promise is not registered until the customer sends the exact confirmation command.
- QA-generated IDs and amounts remain identified as simulated.
- Groq stays active; financial follow-up is deterministic.
- Do not add or run tests. Verify syntax, container startup, and the persisted WhatsApp thread behavior.

## Review Focus

- “100 para la otra semana” retains the amount and asks only for a specific day.
- “El viernes” resolves against `America/Bogota` and proposes a date without registering a promise.
- The explicit confirmation remains the only operation that registers a promise.
- Non-payment questions are not swallowed by an active payment-details stage.
- WhatsApp turns do not wait for an LLM summary after generating their answer.
- A legacy thread whose last assistant response asked for the date recovers its amount after deployment.

---

### Task 1: Deterministic payment terms

**Files:** `src/lla_agent/application/agent.py`

- [ ] Parse explicit or context-allowed bare amounts, ISO dates, relative day counts, and Spanish weekdays.
- [ ] Generate a focused follow-up asking only for the missing amount or date.
- [ ] Keep pending proposal creation separate from explicit promise registration.

### Task 2: Persistent graph stage and legacy recovery

**Files:** `langchain-agent/src/lla_langchain_agent/state.py`, `langchain-agent/src/lla_langchain_agent/workflow.py`

- [ ] Add `payment_flow_stage` and `pending_payment_amount_cents` to checkpoint state.
- [ ] Route short amount/date replies to the deterministic negotiation node while that stage is active.
- [ ] Recover an awaiting-date stage and amount from the previous assistant date question when state is absent.
- [ ] Clear negotiation state on unrelated business intents and after a complete proposal.

### Task 3: Keep WhatsApp responses off the summary model path

**Files:** `langchain-agent/src/lla_langchain_agent/workflow.py`, `langchain-agent/src/lla_langchain_agent/whatsapp_webhook.py`

- [ ] Compact WhatsApp checkpoint history deterministically, preserving the latest complete turns.
- [ ] Log total runtime duration without including customer text, phone numbers, or secrets.

### Task 4: Operational notes and verification

**Files:** `langchain-agent/README.md`

- [ ] Document the multi-message proposal flow and exact confirmation behavior.
- [ ] Compile code, check diff whitespace, rebuild/restart local services, and inspect the active conversation state.
