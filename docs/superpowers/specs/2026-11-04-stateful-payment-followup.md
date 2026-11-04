# WhatsApp payment follow-up state

## Goal

Keep a payment proposal in the same WhatsApp conversation across short replies such as “100 para
la otra semana” followed by “el viernes”, and return a proposal promptly without routing those
financial steps through RAG or long-term memory extraction.

## Decisions

- Store the negotiation stage and an incomplete amount in the existing LangGraph thread checkpoint.
- Parse amount and date deterministically. “La otra semana” remains ambiguous and asks for a day;
  a weekday resolves to its next occurrence in `America/Bogota`.
- Save a complete amount/date only as a pending proposal in SQLite. Register it only after the
  existing explicit confirmation command.
- If the checkpoint has no new stage yet, recover an awaiting-date stage from the previous assistant
  question when it contains a concrete amount. This lets the current stuck thread continue after
  the code is redeployed.
- Do not make WhatsApp wait for an LLM to summarize old history. Compact older messages
  deterministically; SQLite remains the audit history.
- Keep Groq as the configured conversational model. General knowledge answers may still use it.

## Conversation

1. Customer asks about a payment plan; the agent asks for amount and date and saves `awaiting_details`.
2. Customer replies “100 para la otra semana”; the agent saves 10000 cents in the checkpoint and asks
   which day.
3. Customer replies “el viernes”; the agent resolves the date, saves a pending proposal in SQLite,
   and returns the explicit confirmation command.
4. Only that command registers the promise. A date or amount alone never registers it.

## Limits

- New IDs and balances in the QA database remain simulated; no model-generated financial values are
  used.
- The deterministic follow-up parser accepts ISO dates, “en N días”, and Spanish weekday names.
- No package/provider migration is part of this change.
