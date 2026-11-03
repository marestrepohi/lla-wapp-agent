# SQLite billing cases and conversation persistence

## Intent

Give the local WhatsApp agent believable, fictional collections cases for customers `1234`,
`5678`, and `9012`. Each case has phone, home internet, and/or TV plans, overdue invoices, and
prior collection events. The agent should answer from SQLite so account totals and histories remain
consistent across turns.

## Current state

The repository stores each client's aggregate balance, due date, payment promises, escalations, and
message log in `data/collections.sqlite3`. It does not represent service plans or invoice-level
balances. LangGraph already persists conversation state in `langchain-agent/whatsapp-checkpoints.sqlite`
using a stable WhatsApp conversation thread ID. The customer message log and the conversational
checkpoint remain separate: SQL billing records are authoritative for account facts; the checkpoint
preserves conversational context.

## Goals

- Add normalized SQLite records for client service plans, invoices, and collection-history events.
- Seed distinct fictional scenarios for numeric clients `1234`, `5678`, and `9012` with different
  combinations of mobile, internet, and TV services, overdue periods, and collection histories.
- Calculate outstanding balance from open invoice amounts and expose enough account detail for the
  agent to explain the service and invoice breakdown.
- Keep seeding idempotent so restarting the local app does not duplicate invoices or events.
- Retain the existing SQLite conversation checkpointer and message log; do not put balances into
  generated conversation summaries.

## Non-goals

- Connecting to a real carrier, billing system, or customer record.
- Treating a typed client ID or WhatsApp number as real identity verification.
- Replacing SQLite or adding another vector database for this billing data.

## Approaches considered

1. **Normalized service and invoice tables (selected).** Store one row per customer service and
   invoice, with collection events linked to the customer. SQL can sum balances and retrieve
   exact invoice details. This is the selected approach.
2. **Free-text or JSON account descriptions.** Faster to seed, but totals and invoice histories
   would need to be interpreted from unstructured text.
3. **RAG over customer case documents.** Useful for unstructured policy documents, but less reliable
   for balances and invoice calculations that must be exact.

## Design

- Add `client_services` for each fictional product or plan, including service type, plan name,
  monthly price, and service status.
- Add `invoices` with service, billing period, issue and due dates, billed amount, paid amount, and
  status. The outstanding total is `amount_cents - paid_cents` for invoices that are still open.
- Add `collection_events` for fictional reminders, customer replies, payment attempts, and
  payment-promise outcomes. Do not store real personal information.
- Make the repository return the sum of open invoice balances and provide a structured account
  detail query for plans, invoices, and recent collection events. Preserve current response
  compatibility while teaching the WhatsApp agent to use those details when asked.
- Seed three different scenarios: `1234` with mobile and home internet arrears; `5678` with home
  internet and TV plus a missed payment promise; `9012` with mobile and internet, a partial payment,
  and a recent collection interaction. Use fictional names and amounts.
- Keep the existing `clients` rows and identity mapping. Account balances returned to the agent
  come from invoices so duplicate aggregate values cannot drift from the detailed records.
- Keep conversation checkpoints in the existing SQLite checkpointer, keyed to the WhatsApp
  conversation. Keep the normal message audit in the repository database. Billing and collection
  facts are queried from SQL on each turn rather than inferred from a conversation summary.
- QA ID linking remains controlled by `WHATSAPP_QA_ID_LINKING`; this dataset is mock data only.

## Acceptance criteria

- A fresh or existing local SQLite database gains the new tables and the three example cases
  without duplicating seeded rows on restart.
- Each numeric client has multiple service/invoice records and at least one overdue amount.
- The repository's reported total equals the sum of open invoice balances, including partial
  payments.
- After WhatsApp links to one of the numeric IDs, the agent can answer the overall overdue balance
  and identify the related service/invoice details from SQLite on later turns.
- The same WhatsApp thread retains conversational context after the webhook process restarts.

## Calls left to the implementer

Use reasonable fictional PAB amounts and dates relative to the demo dataset. Keep customer names,
phone numbers, and payment details synthetic.
