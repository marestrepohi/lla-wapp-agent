# Conversation Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local WhatsApp agent's replies clearer and more natural without changing financial or identity authorization rules.

**Architecture:** Update the dynamic system prompt used by the LangChain knowledge agent, tune fixed response templates in the deterministic collections service, and document the existing QA identity mapping. Keep identity verification and financial actions outside the LLM.

**Tech Stack:** Python, LangChain `create_agent` middleware, LangGraph workflow, SQLite mock.

**Spec:** `docs/superpowers/specs/2026-11-02-conversation-polish-design.md`

## Global Constraints

- Keep Spanish as the response language and keep WhatsApp replies short and readable.
- Do not change account identity resolution, intent routing, payment proposal validation, or exact confirmation behavior.
- Never claim the user is verified from their phone number alone.
- Do not add dependencies or tests; use static compilation and diff review.

## Review Focus

- Unlinked users must not be told that typing a client ID verifies identity; review the identity response text.
- Memory and image/audio descriptions must remain non-authoritative context; review dynamic prompt instructions.
- Payment amount, date, confirmation command, and tool outcomes must remain unchanged; compare response template edits with the spec.
- QA mapping must be clearly described as a local test bypass; review the environment example and README text.

### Task 1: Polish agent conversation

**Files:**
- Modify: `langchain-agent/src/lla_langchain_agent/agent.py`
- Modify: `src/lla_agent/application/agent.py`
- Modify: `langchain-agent/.env.example`
- Modify: `langchain-agent/README.md`

**Interfaces:** Keep existing public Python function signatures unchanged. The webhook already consumes `WHATSAPP_TEST_SENDER_ID` and `WHATSAPP_TEST_CLIENT_ID` for local QA.

- [x] Update the dynamic system prompt with concise response style, thread continuity, safe memory use, tool authority, and audio/image handling.
- [x] Improve the identity, opt-out, balance, proposal, confirmation, escalation, and billing-error response wording without changing their logic or interpolation values.
- [x] Document that QA can map the controlled WhatsApp sender to `C001` with the existing test-only environment variables; explain this is not OTP verification.
- [x] Compile/import check passed; local and ngrok `/health` both returned `{"status":"ok"}`. Inspected the response changes; no financial logic or confirmation command was changed.

No automated tests were added or run, per the task's verification constraint.
