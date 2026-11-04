# WhatsApp Delivery Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make WhatsApp processing idempotent across SQS retries, isolate conversation data, and surface delivery failures through CloudWatch.

**Architecture:** Keep Meta Cloud API → API Gateway → ingress Lambda → per-sender SQS FIFO → worker Lambda → LangGraph/Groq. Add a dedicated DynamoDB conversation table for transcript audit and an inbound idempotency/outbox state machine; retry transient failures with bounded message visibility and quarantine terminal or ambiguous sends in the existing FIFO DLQ. Add CloudWatch alarms and an optional SNS email subscription.

**Tech Stack:** Python 3.12, boto3, AWS SAM/CloudFormation, SQS FIFO, Lambda, DynamoDB, CloudWatch, SNS, KMS, LangGraph.

**Spec:** `docs/superpowers/specs/2026-11-03-whatsapp-delivery-reliability-design.md`

## Global Constraints

- Do not deploy AWS resources or change live AWS state.
- Keep Meta Cloud API, Groq, Qdrant, the stable hashed WhatsApp thread ID, and QA-only linking behavior.
- Keep worker timeout at 120 seconds, queue visibility at 720 seconds, batch size at one, and per-sender FIFO grouping.
- Use inbound Meta `wamid` as the durable idempotency key; never log message bodies, phone numbers, tokens, or secret values.
- Preserve the existing SQS partial-batch-failure response and max receive count of five.
- Audit visible inbound/outbound content for 90 days; keep LangGraph checkpoints for 30 days; keep Qdrant limited to selected safe semantic memories.
- Do not add or run tests; validate changed Python with syntax compilation and validate SAM/CloudFormation offline.

## Review Focus

- Duplicate SQS delivery after `SENT` must not invoke LangGraph or send another WhatsApp reply.
- Retry of `REPLY_READY` must reuse the stored response instead of spending another Groq call.
- An expired `SENDING` lease must become `SEND_UNKNOWN` and must not automatically send again.
- Retryable failures must shorten only the current message visibility after model/provider work has stopped; permanent and ambiguous outcomes must reach the FIFO DLQ through the existing redrive policy.
- A permanently failed sender must be paused so later messages from that sender do not reach LangGraph until an operator reconciles and replays them in FIFO order.
- A DLQ send failure must keep the original source message retryable.
- DynamoDB audit writes must use the new conversation table while customer/billing operations remain on `CustomerDataTable`.
- Alarms must observe per-record failures, not only Lambda invocation errors, and must not log or expose customer content.

---

### Task 1: Add conversation audit and idempotency/outbox persistence

**Files:**
- Create: `aws-demo/src/aws_demo/message_store.py`
- Modify: `src/lla_agent/persistence/dynamodb.py`
- Modify: `aws-demo/src/aws_demo/agent_runtime.py`

**Interfaces:**
- `MessageClaim` is an immutable result with `status` in `claimed | reply_ready | already_sent | in_progress | send_unknown | failed | conversation_paused` and optional `response_text`.
- `DynamoDBMessageStore(table_name, *, dynamodb=None, config=None)` exposes `claim(sender_id, message_id, lease_seconds)`, `quarantine_poison(group_id_hash, sqs_message_id)`, `save_reply(sender_id, message_id, response_text)`, `mark_sending(sender_id, message_id)`, `mark_sent(sender_id, message_id, provider_message_id)`, `mark_send_unknown(sender_id, message_id)`, `mark_retryable(sender_id, message_id)`, and `mark_failed(sender_id, message_id, category)`. `claim` atomically creates the sender's durable active-turn lock and event row; only `mark_sent` clears the lock, in the same transaction that marks the event complete. `quarantine_poison` uses the already hashed FIFO group ID and a digest of the SQS message ID to create a locked terminal row without storing a phone number.
- `DynamoDBRepository(table_name, *, conversation_table_name=...)` uses `table_name` for customer/business data and the optional dedicated table for `record_message` audit writes.
- The worker creates the store from `DYNAMODB_CONVERSATION_TABLE`.

- [x] Create a focused message store that uses conditional and transactional DynamoDB writes, leases, idempotent reply storage, a durable per-sender active-turn lock, and 90-day TTL attributes for event/audit rows. The lock has no TTL and is released only by the successful `SENT` transaction.
- [x] Route only audit `record_message` writes to the conversation table; preserve all business reads/writes on the customer table.
- [x] Wire the new table name into the AWS runtime while leaving local SQLite code unchanged.
- [x] Review conditional-write and expired-lease behavior for duplicate, concurrent, and replayed events, including an exhausted retry/crash where the worker never records a terminal state.
- [x] Run `python -m compileall` on changed Python files.

### Task 2: Make the worker idempotent and classify retry outcomes

**Files:**
- Modify: `aws-demo/src/aws_demo/worker.py`

**Consumes:** Task 1's `DynamoDBMessageStore` interface.

- [x] Claim each inbound Meta ID before normalizing media or invoking the graph; quarantine malformed SQS records using their hashed FIFO group ID so later turns cannot pass them.
- [x] Skip `already_sent`; wait/retry `in_progress`; reuse `reply_ready`; only a fresh/expired processing claim runs the graph.
- [x] Persist the generated response, mark `SENDING`, call Meta once, parse its outbound message ID, and mark `SENT` after HTTP success.
- [x] Load a WhatsApp access token independently before sending a saved `REPLY_READY` response on a cold Lambda start; this path must not require Groq.
- [x] For `InboundMediaError`, persist/send its direct guidance text without passing that text back as user input to the model.
- [x] On a definite Meta `429` response, transition back to `REPLY_READY`; on any later turn from a sender whose active-turn lock belongs to an earlier inbound ID, skip model/provider work.
- [x] Mark Meta `408`/`5xx` responses and ambiguous post-send transport failures `SEND_UNKNOWN` and quarantine them without an automatic resend.
- [x] Classify retryable/permanent outcomes; apply bounded exponential per-message visibility delays to retryable failures. For Meta sends, retry only `429` and failures proven to happen before request transmission.
- [x] For terminal/unknown outcomes, pause the sender and return item failure until SQS's existing redrive policy moves messages to the FIFO DLQ; do not manually copy to the DLQ.
- [x] Emit structured start/agent-complete/send/failure events with hashed inbound IDs, attempt counts, categories, status, host, and durations only.
- [x] Preserve partial-batch behavior and stop after the first FIFO record failure.
- [x] Run `python -m compileall` on the worker.

### Task 3: Provision separated storage, safe retry settings, and alarms

**Files:**
- Modify: `aws-demo/template.yaml`

- [x] Add encrypted on-demand `ConversationMessagesTable` with `pk`/`sk` keys and 90-day `expires_at` TTL.
- [x] Pass the conversation table name to the worker; scope worker IAM to the new table including `dynamodb:TransactWriteItems`.
- [x] Retain worker timeout 120, queue visibility 720, batch size one, per-sender FIFO, and max receive count five.
- [x] Add a CloudWatch Logs metric filter for structured per-record failures and alarms for message age, visible/in-flight backlog, DLQ messages, record failures, Lambda errors/throttles, and near-timeout duration.
- [x] Add an alarm SNS topic encrypted with a retained customer-managed KMS key, scoped CloudWatch/SNS key policy, and optional `AlarmNotificationEmail` subscription parameter.
- [x] Run `sam validate --lint`.

### Task 4: Document operation and recovery

**Files:**
- Modify: `aws-demo/README.md`
- Modify: `README.md`

- [x] Explain checkpoint history versus 90-day user-visible audit versus Qdrant semantic memory versus SQS pending work.
- [x] Document idempotency states, automatic retryable failures, permanent/ambiguous DLQ handling, sender lock behavior, the maintenance-window DLQ replay procedure, and the manual step required for `SEND_UNKNOWN`.
- [x] Document alarm meanings and optional email subscription confirmation.
- [x] State that the change is code/IaC only and describe the deployment action required to create the new DynamoDB table and alarms.
- [x] Run Python syntax compilation and `sam validate --lint`; do not deploy.
