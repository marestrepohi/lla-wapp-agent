# WhatsApp delivery reliability and observability

## Intent

Prevent a redelivered SQS event from regenerating or repeatedly sending the same reply, keep a
complete user-visible conversation audit, and make queue failures visible to the operator. Preserve
the current Meta Cloud API, API Gateway, SQS FIFO, Lambda, LangGraph, Groq, DynamoDB, and Qdrant
architecture.

## Goals

- Preserve per-sender ordering in the inbound FIFO queue and the current 120-second worker timeout.
- Keep the queue visibility timeout at 720 seconds (six times the Lambda timeout) and batch size at
  one unless measured latency later justifies changing both together.
- Store conversation audit rows separately from customer and billing records.
- Use Meta's inbound message ID as a durable idempotency key across webhook and SQS retries.
- Persist an agent reply before sending it so a retry reuses that reply instead of invoking Groq again.
- Record the outbound Meta message ID and mark the event complete after Meta accepts the send.
- Treat a process crash or transport timeout after a send starts as an unknown delivery outcome; do
  not automatically send again. Move that event to the FIFO DLQ for inspection and replay.
- Retry known transient failures with capped exponential visibility delays, and let the existing SQS
  redrive policy move repeatedly failing messages to the FIFO DLQ.
- Pause a sender's conversation after a permanent/ambiguous failure. Later messages from that sender
  must not be run through the agent while an earlier turn is unresolved; they are retried without
  model/API work and eventually join the FIFO DLQ.
- Emit structured worker events without message bodies, phone numbers, tokens, or secret values.
- Add CloudWatch alarms for queue age/depth, DLQ contents, worker record failures, Lambda errors,
  throttles, and near-timeout duration. Allow an optional email subscription for the alarm topic.
- Keep existing customer identity, QA-only linking, billing, and LangGraph thread behavior unchanged.
- Do not deploy or mutate live AWS resources as part of this change.

## Non-goals

- Replacing Groq with Bedrock, migrating the WABA to AWS End User Messaging, or adding Connect/Lex.
- Guaranteeing exactly-once delivery across DynamoDB and Meta's HTTP API. Meta can accept a request
  while the worker loses the response; that ambiguous outcome will be quarantined instead of retried.
- Building an operator dashboard or automatic DLQ replay workflow.
- Changing local SQLite/Chainlit behavior.

## Approaches

1. **Add a durable conversation/processing table and an outbox state machine (selected).** A dedicated
   DynamoDB table stores audit messages and an idempotency record for each inbound Meta message ID.
   The worker claims the record, runs the graph once, saves the reply, then sends and records Meta's
   returned message ID. This uses the existing AWS services and prevents ordinary SQS retries from
   rerunning the model or resending completed work.
2. **Keep the current single table and add an in-memory cache.** This is a smaller edit, but the cache
   is lost on Lambda recycle and cannot protect against concurrent or delayed SQS deliveries.
3. **Replace the messaging path with AWS End User Messaging and SNS.** This changes WhatsApp
   onboarding and message APIs, and its SNS destination does not provide the same per-sender FIFO
   ordering. It is not needed to fix the current retry behavior.

## Design

### Storage and idempotency

- `CustomerDataTable` remains the source for profiles, services, invoices, identity links, promises,
  opt-outs, and escalations.
- A new on-demand `ConversationMessagesTable` uses the existing `pk`/`sk` key shape, server-side
  encryption, and the `expires_at` TTL attribute. It stores inbound/outbound user-visible message
  audit rows for 90 days and idempotency/outbox state rows for 90 days.
- Conversation audit partition keys remain SHA-256-derived from the channel identity. Audit message
  sort keys remain deterministic from direction and message ID so retries do not duplicate audit rows.
- An inbound ledger row is keyed by a SHA-256 digest of the Meta `wamid` and starts in `PROCESSING`
  with a lease. The state machine is `PROCESSING` → `REPLY_READY` → `SENDING` → `SENT`, with
  `SEND_UNKNOWN` and `FAILED` terminal/inspection states. A stored `REPLY_READY` response is reused
  on retries.
- Claiming an inbound event atomically sets a durable per-sender turn lock (`active_message_id`) and
  an event ledger row. A live lease prevents a second worker from running the graph for the same
  inbound ID. An expired `PROCESSING` lease can be reclaimed. An expired `SENDING` lease becomes
  `SEND_UNKNOWN` and is quarantined rather than resent. The sender lock remains held until Meta
  accepts a reply and the ledger is committed to `SENT`.
- `SENT` events are acknowledged without running the graph or sending again. `FAILED` and
  `SEND_UNKNOWN` events are not automatically processed again; they remain quarantined until an
  operator reconciles/resets the ledger before redriving them. If SQS exhausts receives after a
  transient failure or a crashed invocation, the durable sender lock still blocks later turns even
  though that event may have reached the DLQ without a terminal ledger state.
- While a sender lock is held by an earlier inbound ID, later source messages from that sender are
  returned as failures without calling Groq or Meta. SQS's FIFO order and redrive policy then move
  each blocked message to the FIFO DLQ in turn. A successful `SENT` transaction clears the lock;
  terminal, exhausted, and ambiguous outcomes keep it held. Claiming the first turn and releasing a
  completed turn use conditional transactional writes so concurrent duplicate events cannot bypass
  the lock.
- LangGraph checkpoints remain in the existing `AgentCheckpointTable` with the same stable hashed
  thread ID and 30-day TTL. Audit storage remains the canonical record of user-visible messages.
- When an audio/image event cannot be processed, persist and send the direct guidance response
  without feeding that guidance back into the agent as customer input. Audit a safe placeholder
  such as `[audio adjunto]`; do not store media URLs, IDs, or bytes.
- Qdrant remains a separate optional store of selected safe semantic memories; it is not a transcript.

### Queue and worker behavior

- Keep `MessageGroupId` derived from the sender so messages received for one sender remain ordered.
- If an internal poison record has no usable Meta sender/message ID, persist a terminal lock using
  the already-hashed SQS `MessageGroupId` and a digest of the SQS message ID. This keeps later turns
  for that sender paused without storing the phone number. The webhook ingress continues to reject
  malformed Meta events before queueing them.
- Keep the SQS event source `BatchSize: 1` and `ReportBatchItemFailures`.
- Keep Lambda timeout 120 seconds and queue visibility timeout 720 seconds. AWS's six-times rule is
  retained to avoid a second consumer receiving work while the first Lambda invocation is still
  running.
- For retryable failures, stop all model/provider work, call `ChangeMessageVisibility` as the final
  processing action, then immediately return the item in `batchItemFailures`. Use delays 15, 30, 60,
  120, and at most 240 seconds; SQS redrive remains capped at five receives. If the event is moved
  to the DLQ before succeeding, its sender lock remains held and prevents later turns from running.
- For known permanent errors, store `FAILED`, pause the sender, and return the source item as failed.
  On subsequent receives, skip model/provider work and keep returning failure until SQS moves it to
  the existing FIFO DLQ. This uses the queue's configured redrive path and avoids a second manual
  DLQ-send operation.
- A sender remains paused from its first claimed turn until that turn is confirmed `SENT`. This also
  covers exhausted transient failures and hard crashes where the worker cannot write a terminal
  failure state. Later messages from that sender receive no agent response; they are retried without
  external calls and eventually move to the same FIFO DLQ.
- During agent/model processing, `429`, `408`, `5xx`, throttling, and connection failures are
  retryable. For the Meta send, only a definite `429` response and failures known to happen before
  the request is sent (for example, connection establishment) are safe to retry automatically.
- A Meta `408`/`5xx` response, read/write timeout, or protocol failure after a WhatsApp send begins
  is `SEND_UNKNOWN`; the worker quarantines it instead of risking a duplicate by blindly repeating
  the send. Other Meta `4xx` responses are permanent `FAILED` outcomes.
- When a known retryable Meta `429` response occurs after `REPLY_READY`, transition back to
  `REPLY_READY` before returning the SQS failure so the next attempt resends the saved reply without
  re-running LangGraph/Groq.
- Every worker event includes a short hash of the inbound ID, receive count, status/category, and
  elapsed time only. Message text, sender number, access tokens, and secret values are never logged.

### Monitoring

- Add alarms for oldest message age (300 seconds), visible queue backlog (10 messages), in-flight
  backlog (10 messages), any visible DLQ message, worker record-failure metric, Lambda errors,
  Lambda throttles, and worker duration at 100 seconds.
- Add a JSON log metric filter for `queue_message_failed`; this detects per-record failures even
  when Lambda returns a successful partial-batch response.
- Publish alarm actions to an SNS topic. An optional `AlarmNotificationEmail` parameter creates an
  email subscription; the subscriber must confirm it after deployment. Encrypt the topic with a
  retained customer-managed KMS key whose policy grants only this account's CloudWatch alarms and
  SNS access to the topic.
- Keep log retention and queue retention values as currently configured.

## Risks and constraints

- DynamoDB and the Meta API cannot commit atomically. A crash after Meta accepts but before DynamoDB
  records `SENT` is handled as `SEND_UNKNOWN`; this prevents automatic duplicate replies but may
  require manual reconciliation.
- The new table receives new audit rows after deployment; historic audit items remain in
  `CustomerDataTable` until their existing TTL expires. No migration or deletion is needed.
- SQS FIFO redrive moves DLQ messages oldest-first, but new source sends can interleave with redriven
  messages. Recovery therefore requires a maintenance window: stop webhook ingress, wait until the
  source queue is empty (blocked later turns will have moved to the DLQ), disable the worker event
  source mapping, redrive the DLQ to the source queue and wait for the redrive task to finish, reset
  only the oldest blocked ledger item for each sender, then re-enable the worker and webhook ingress.
  Do not clear a sender lock or replay only a later message. An ambiguous `SEND_UNKNOWN` must be
  reconciled against Meta's message history before deciding whether to mark it sent or allow a resend.
- The message audit and stored reply contain customer text. Their 90-day TTL and encryption settings
  must remain explicit and access must stay limited to the worker role.
- CloudWatch alarms can be created without an email parameter, but delivery to an operator requires
  an email subscription to be confirmed after deployment.
