# WhatsApp agent runtime with DynamoDB

## Intent

Run the existing LangGraph collections agent behind the AWS WhatsApp webhook. DynamoDB will hold
customer cases, WhatsApp-to-customer links, message audit records, and LangGraph checkpoints. Groq
remains the chat model. Qdrant remains the vector service for public-page RAG and safe, customer-scoped
semantic memories.

## Goals

- Process Meta webhook messages through the existing API Gateway, Lambda ingress, SQS FIFO, and Lambda
  worker path.
- Ask for a customer ID when a WhatsApp sender without a linked profile requests account information.
- Look up the ID in DynamoDB and conditionally associate it with the sender for QA.
- Keep one stable LangGraph conversation thread per WhatsApp sender across profile linking and Lambda
  restarts.
- Read billing facts from DynamoDB tools and write confirmed promises, opt-outs, escalations, and
  message audit records to DynamoDB.
- Store LangGraph checkpoints in DynamoDB and keep the Groq, WhatsApp, and optional Qdrant credentials
  in Secrets Manager.
- Load demo billing cases through an explicit idempotent seed command.

## Non-goals

- Deploying CloudFormation or changing live AWS resources during this code change.
- Importing real customer data or replacing the mock records with a production billing feed.
- Treating a customer ID typed into WhatsApp as production identity proof. This remains a QA-only link
  flow; production account access needs a trusted verification step such as OTP.
- Adding a payment gateway, campaign scheduler, or human-agent console.

## Approaches

1. **Use DynamoDB for both the business repository and LangGraph checkpoints (selected).** This keeps
   the existing queue/Lambda design and replaces the local SQLite-only persistence at the AWS boundary.
   A stable hashed sender thread ID preserves conversation context while the account link is added.
2. **Move only LangGraph checkpoints and leave customer cases in SQLite.** This does not work for the
   AWS worker because Lambda's local filesystem is not a shared durable customer database.
3. **Build a custom LangGraph saver over the business table.** This duplicates checkpoint semantics
   already provided by `langgraph-checkpoint-aws` and couples conversation records to case records.

## Design

- `DynamoDBRepository` implements the persistence contract consumed by the existing business tools.
  A composite `pk`/`sk` table stores profiles, services, invoices, collection events, promises,
  pending promises, opt-outs, identity links, and message audit records.
- A separate `PK`/`SK` table is reserved for `DynamoDBSaver` checkpoints.
- The runtime thread key is SHA-256 of channel and conversation ID only. It does not include the
  linked client ID, so the same sender retains one conversation before and after ID verification.
- QA ID linking checks that a profile exists and writes a one-way sender-to-client mapping with a
  conditional DynamoDB write. A sender already linked to another profile cannot switch profiles.
- The AWS worker normalizes text, audio, and image messages using the existing WhatsApp media adapter,
  invokes the shared runtime, then sends its response through the Meta Cloud API.
- The AWS runtime uses Qdrant collections separate from local Chroma: public website chunks and
  customer-scoped safe memories. Account balances and invoices remain authoritative in DynamoDB.
- Secrets are referenced by ARN. Runtime code retrieves values without logging them. The webhook
  secret contains `app_secret` and `verify_token`; a separate WhatsApp secret contains
  `access_token`; the Groq secret contains `api_key`. Qdrant configuration is optional for
  deployments that do not enable RAG/memory.
- The AWS QA flag defaults off in the template. The demo deploy command explicitly enables it for
  synthetic seeded profiles.

## Risks and constraints

- A typed customer ID alone does not prove the WhatsApp sender owns the account. Never enable QA
  linking against real billing data.
- DynamoDB cases and checkpoint state use separate tables and separate IAM permissions.
- The worker is still a Lambda container image. Local index files are not its durable RAG backend;
  Qdrant Cloud is used for retrieval.
- This change prepares deployable code and infrastructure but does not deploy the stack or create
  Secrets Manager values.
