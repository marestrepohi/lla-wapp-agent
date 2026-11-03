# Conversation polish design

## Intent

Make WhatsApp replies feel clear, brief, and continuous while preserving the prototype's identity
checks and deterministic financial rules.

## Chosen approach

Use a concise Spanish system prompt for the LangChain knowledge/RAG agent, and edit the deterministic
collection response templates directly. This keeps financial amounts and state changes controlled
by the existing SQLite workflow. A post-processing LLM could rewrite business facts, while real OTP
verification requires a registered phone source and delivery service that this local prototype does
not have.

## Scope

- Add response style, continuity, memory, tool-use, and media guidance to the existing dynamic prompt.
- Make the unlinked-identity response accurate for this demo; do not ask the user to type a client ID
  as if that verified them.
- Improve the wording of deterministic collection replies without changing intent routing,
  proposal matching, confirmation requirements, or persistence behavior.
- Document the existing test-only sender-to-client mapping for QA (`C001`).

## Non-goals

- Implement OTP, account recovery, a production identity provider, or voice replies.
- Change balances, client records, payment rules, or the exact confirmation command.

## Verification

Use static compilation and inspect the final diff. Do not add or run tests in this change.
