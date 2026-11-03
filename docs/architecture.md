# Proyecto Ágil — 100% AWS production architecture

## Scope and assumptions

This design supports outbound and inbound collections conversations for residential and
business subscribers in Panama. The billing/CRM systems expose authenticated APIs or CDC
events for balance, customer identity, eligibility, and payment status. WhatsApp Business
Cloud API is the production channel. The local repository and deterministic workflow in this
prototype are simulations, not financial systems.

Assumptions: customer consent and contact eligibility are available; a billing account has a
stable ID; balances are authoritative only in billing; payment promises require explicit
confirmation; human agents have a queue/CRM; legal/compliance owns contact hours, retention,
language, and opt-out policy.

## Runtime flow

1. A campaign service selects eligible overdue accounts from billing/CRM, applies contact
   windows and consent rules, and emits an idempotent campaign message to **SQS**.
2. A sender **Lambda** consumes SQS, retrieves the WhatsApp token from **Secrets Manager**, and
   calls the Meta WhatsApp Business API. Delivery status webhooks go through **API Gateway**.
3. API Gateway validates the webhook route and forwards the event to an inbound **Lambda**.
   Signature verification, message-ID deduplication, rate limits, and opt-out checks happen
   before model invocation.
4. The Lambda loads conversation state from **DynamoDB** (or Aurora for relational reporting),
   resolves the sender to a customer through the CRM identity service, and invokes the model
   layer (Amazon Bedrock by default; an approved model endpoint can be substituted).
5. The model is constrained to a tool schema and policy prompt. Tool calls execute in a
   separate service layer/Lambda: `consultar_saldo` reads billing; `registrar_promesa_pago`
   writes through an idempotent billing API; `escalar_a_humano` creates a CRM/work-queue item.
   Conversation state is short-term memory; customer-scoped preferences/context may be stored
   as long-term memory. Neither conversation memory nor vector search is financial authority.
6. The response is policy-checked, persisted, and queued to the sender. High-risk intents,
   repeated failures, disputes, vulnerability signals, and low confidence go to a human queue.
   **EventBridge** emits audit and business events. **Step Functions** coordinates longer
   promise follow-up and payment reconciliation.

## Local prototype persistence

The local prototype uses SQLite to make the account flow reproducible without carrier or billing
credentials. `data/collections.sqlite3` contains mock clients, service plans, invoices, collection
events, promises, and a message audit. For numeric QA profiles, the open balance is calculated from
invoice amount minus payments applied; chat memory is never used to calculate it. The
`langchain-agent/whatsapp-checkpoints.sqlite` file stores LangGraph state for each WhatsApp thread,
including recent conversation context. Qdrant remains a separate store for optional semantic
memories, not financial facts. These local databases are simulations and do not replace billing/CRM
systems in the production architecture below.

## AWS components and rationale

- **API Gateway**: public WhatsApp webhook, throttling, WAF integration, request validation.
- **Lambda**: stateless webhook, orchestration, tool functions, and adapters; container images
  are useful when model/runtime dependencies are large.
- **Amazon Bedrock**: managed model layer in-region where available, with model access controls,
  prompt/version configuration, and no direct database authority. A smaller model can classify
  intent and a stronger model can draft only within policy.
- **DynamoDB**: conversation state, opt-out records, deduplication keys, tool audit events,
  escalation status. Partition by customer/channel and use conditional writes for idempotency.
- **Aurora Serverless v2**: relational reporting, campaign segmentation, and integration views
  where DynamoDB access patterns do not fit. The billing platform remains authoritative.
- **SQS**: durable outbound work queue, retries, dead-letter queue, back-pressure, and one
  message ID per attempted send.
- **EventBridge**: domain events (`PromiseCreated`, `OptOutRecorded`, `EscalationCreated`,
  `PaymentReconciled`) for CRM, analytics, and operations without tight coupling.
- **Step Functions**: wait/recheck workflows for promised payment, retry policies, and
  human-escalation SLAs.
- **Secrets Manager/KMS**: WhatsApp credentials, API keys, and encryption keys; no secrets in
  environment files or logs.
- **CloudWatch/X-Ray**: structured logs with redaction, metrics, alarms, traces, dashboards,
  and audit correlation IDs.
- **IAM**: least-privilege execution roles, separate deployment/runtime roles, explicit deny
  for unauthorized data access, and permission boundaries.
- **S3/Glue/Athena (optional)**: encrypted, access-controlled analytics archive after retention
  and PII minimization review.

## Safety and operational controls

- **Identity**: bind WhatsApp number to a CRM identity, require step-up verification for
  sensitive changes, and never reveal balances for an unknown/unvalidated client.
- **Truthfulness**: every balance is a live billing tool result. If billing is unavailable,
  state that fact and escalate; never guess or use vector memory.
- **Promises**: the model may propose; only an explicit affirmative confirmation allows the
  write tool. Billing writes include an idempotency key and optimistic concurrency.
- **Disputes and discounts**: disputes, unauthorized discount requests, legal threats,
  vulnerability, fraud signals, and repeated tool errors route to humans. The agent cannot
  change tariffs or waive debt.
- **Opt-out**: process STOP and local-language variants before model invocation, persist a
  channel/customer suppression record, cancel queued outbound work, and honor it across
  campaigns. Only compliant transactional messages may be considered later.
- **Privacy**: minimize prompts, redact PII from logs, encrypt at rest/in transit, configure
  retention/deletion, restrict cross-border access, and obtain legal approval for Panama
  contact/recording requirements.
- **Resilience**: timeouts, circuit breakers, bounded retries, SQS DLQs, replay-safe IDs,
  billing read cache only for availability (never as an unmarked authority), and human
  fallback.
- **Prompt/model security**: treat user text as untrusted, tool allow-lists, schema validation,
  output moderation, prompt/version pinning, adversarial tests, and no arbitrary code/tool
  execution.

## Governance and metrics

Campaign operators configure eligibility, contact windows, rate limits, templates, and model
versions through an authenticated internal service with approvals. Changes are versioned and
audited. Compliance can pause a campaign globally.

Business metrics: contact/delivery rate, valid identity rate, payment conversion, promise kept
rate, recovered amount, cost per recovered account, escalation rate, dispute rate, opt-out rate,
and time to human response. Safety metrics: unsupported-claim rate, tool error rate, policy
violations, duplicate writes, PII exposure alerts, and model fallback rate. Technical metrics:
latency, Lambda errors/throttles, SQS age/DLQ depth, API 5xx, Bedrock quota, and billing
dependency availability. Report segmented metrics, never only an aggregate recovery number.

## Rollout

Start in shadow mode with no outbound sends, then a small consented cohort, human approval for
every promise, and daily compliance review. Expand only after offline scenario tests, red-team
tests, billing reconciliation, opt-out drills, incident runbooks, and measurable guardrail
success. The local prototype intentionally simulates these integrations with SQLite.
