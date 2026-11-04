# DynamoDB WhatsApp runtime implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the AWS SQS worker to the shared WhatsApp LangGraph agent with DynamoDB-backed cases and conversation checkpoints.

**Architecture:** Keep the API Gateway → ingress Lambda → SQS FIFO → worker Lambda path. The worker invokes the common graph with a DynamoDB repository, DynamoDB checkpointer, Groq, WhatsApp media normalization, Meta Cloud API replies, and optional Qdrant retrieval.

**Tech Stack:** Python 3.12, AWS SAM, Lambda container images, SQS FIFO, DynamoDB, Secrets Manager, LangGraph, Groq, Qdrant.

**Spec:** `docs/superpowers/specs/2026-11-03-dynamodb-whatsapp-runtime-design.md`

## Global Constraints

- Do not deploy AWS resources as part of this code change.
- Keep the customer ID link mode opt-in and limited to synthetic QA data.
- Store money as integer cents and query exact customer records from DynamoDB.
- Keep DynamoDB checkpoints separate from customer-case items.
- Do not log message bodies, phone numbers, API tokens, or secret values.
- Keep a stable hashed conversation thread ID that does not change after client linking.
- Do not add or run tests in this task; use syntax and template validation only.

## Review Focus

- A sender supplying an unknown ID must not get linked or see account information: verify DynamoDB lookup is required.
- A sender already linked to another profile must not switch profiles: use conditional writes and preserve conflict behavior.
- A message retried by SQS must not corrupt checkpoint history or promise writes: preserve message IDs and business idempotency.
- A missing optional Qdrant secret must not prevent billing queries from working.
- Failure to send through Meta must be visible as a retryable worker failure without logging credentials or message content.

---

### Task 1: Generalize the persistence contract and stabilize threads

**Files:**
- Modify `src/lla_agent/application/ports.py`
- Modify `src/lla_agent/application/agent.py`
- Modify `src/lla_agent/tools/business.py`
- Modify `langchain-agent/src/lla_langchain_agent/agent.py`
- Modify `langchain-agent/src/lla_langchain_agent/workflow.py`
- Modify `langchain-agent/src/lla_langchain_agent/runtime.py`

- [ ] Define a repository protocol for the methods consumed by tools and workflow.
- [ ] Replace SQLite-only type annotations with that protocol while keeping SQLite as the local default.
- [ ] Make the checkpoint thread hash depend on channel and conversation ID, not client ID.
- [ ] Allow the AWS runtime to inject an externally managed checkpointer while local interfaces retain SQLite.
- [ ] Run Python syntax compilation on the changed files.

### Task 2: Implement the DynamoDB customer repository and QA seed data

**Files:**
- Create `src/lla_agent/persistence/dynamodb.py`
- Create `aws-demo/scripts/seed_demo_cases.py`
- Modify `aws-demo/template.yaml`

- [ ] Use boto3's DynamoDB resource interface and a `pk`/`sk` customer-data table.
- [ ] Implement profile lookup, external-ID lookup/link, balance/account details, payment promises,
  pending promises, escalations, opt-outs, and message audit writes.
- [ ] Use a conditional write for sender-to-client linking and promise idempotency.
- [ ] Seed the synthetic QA profiles, services, invoices, and collection events idempotently.
- [ ] Provision separate on-demand case and LangGraph checkpoint tables with encryption.
- [ ] Run Python syntax compilation and `sam validate --lint`.

### Task 3: Add a remote Qdrant corpus option

**Files:**
- Create `langchain-agent/src/lla_langchain_agent/qdrant_rag.py`
- Create `aws-demo/scripts/index_public_pages.py`
- Modify `langchain-agent/src/lla_langchain_agent/workflow.py`

- [ ] Implement the same `search` interface as the local `WebCorpus` using a distinct Qdrant public-pages collection.
- [ ] Preserve source URL, title, and chunk metadata in indexed documents.
- [ ] Reuse the configured embedding model for both ingestion and retrieval.
- [ ] Keep local Chainlit indexing on Chroma and allow the AWS worker to select Qdrant.
- [ ] Run Python syntax compilation.

### Task 4: Connect the AWS worker, secrets, and WhatsApp replies

**Files:**
- Modify `aws-demo/src/aws_demo/worker.py`
- Create `aws-demo/src/aws_demo/agent_runtime.py`
- Create `aws-demo/worker.Dockerfile`
- Modify `aws-demo/template.yaml`
- Modify `langchain-agent/pyproject.toml` and `langchain-agent/uv.lock`

- [ ] Build the worker image from the repository root so it can include the shared packages.
- [ ] Initialize DynamoDB repository and `DynamoDBSaver` from table names and Lambda IAM role credentials.
- [ ] Load Groq and WhatsApp secrets by ARN and create one runtime per Lambda execution environment.
- [ ] Normalize text, audio, and images, call `AgentRuntime`, and send text replies to Meta.
- [ ] Route failed queue records through Lambda's partial-batch failure response.
- [ ] Add scoped IAM access to the two DynamoDB tables and referenced Secrets Manager secrets.
- [ ] Run Python syntax compilation and `sam validate --lint`; do not deploy.

### Task 5: Document deployment and QA operation

**Files:**
- Modify `aws-demo/README.md`
- Modify root `README.md`

- [ ] Document required secret JSON keys, DynamoDB table names, seed command, QA deployment parameter,
  page-ingestion command, and an end-to-end WhatsApp QA example.
- [ ] State that ID-only linking is for synthetic QA profiles and production requires a trusted verifier.
- [ ] Document the exact SAM build/deploy sequence without executing it.
