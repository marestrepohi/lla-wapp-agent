# Assessment compliance map

| Assessment requirement | Implementation/evidence | Status |
|---|---|---|
| WhatsApp customer channel | `aws-demo/` validates Meta signatures and routes messages through API Gateway, SQS FIFO, and a worker that replies using Cloud API | Implemented in the AWS branch; not deployed |
| AWS runtime architecture | API Gateway, Lambda images, SQS, DynamoDB, Secrets Manager, CloudWatch, and IAM are defined in `aws-demo/template.yaml`; Groq and optional Qdrant remain external | Infrastructure code prepared; not deployed |
| Agentic AI, not only a decision tree | `langchain-agent/` combines LangGraph policy routes with Groq `create_agent` and customer/web retrieval tools; AWS worker uses this same graph | Implemented against synthetic cases; not deployed |
| Obtain customer/context data | `DynamoDBRepository` stores customer profiles, billing cases, and WhatsApp identity links; the explicit seed command loads synthetic QA data | Implemented; awaiting table deployment and seed |
| Read/write existing systems | `BillingTool`, `PaymentsTool`, and `EscalationTool` use the repository interface; AWS maps it to DynamoDB | Implemented with synthetic DynamoDB schema |
| Minimum flow: message, balance, decide/escalate | `demo/run_demo.py` and scenario tests invoke the shared `AgentRuntime` | Implemented locally with mock data |
| Tool use | Exact tools: `consultar_saldo`, `registrar_promesa_pago`, `escalar_a_humano` | Implemented |
| Negotiable debt | Pending proposal after balance read; no write before confirmation | Implemented |
| Confirmed promise | Exact confirmation command must match the pending proposal amount and date before the write tool | Implemented |
| Disputed balance | Balance is read first, then an escalation is created | Implemented |
| Unauthorized discount | No discount is invented or applied; human escalation | Implemented |
| Human escalation | Auditable escalation table and tool response | Implemented |
| Unknown client / identity | No balance disclosure or billing call for an unknown client | Implemented |
| Tool error | Honest failure message, no invented amount, escalation | Simulated by `CERR` |
| Opt-out | STOP/local variants persist suppression and prevent later collection content | Implemented locally |
| No vector memory as financial authority | Chroma locally and optional Qdrant in AWS serve web RAG and safe client-scoped memory; account facts and balances come from SQLite locally or DynamoDB in AWS | Implemented |
| Simple executable README | Root README includes setup and runtime demo/test commands | Documented |

## Explicit simulations and limitations

- Client balances, identities, due dates, and billing outage (`CERR`) are synthetic mock data loaded to
  DynamoDB only by an explicit seed command.
- Telegram polling is optional and requires a bot token; it is not WhatsApp production
  integration.
- Telegram and Chainlit share the runtime contract locally. The Telegram adapter needs a trusted
  external-ID-to-client mapping before it can access account tools.
- The local WhatsApp webhook still supports ngrok-based testing. The AWS ingress validates Meta POST
  signatures and the worker processes text, audio, and images. ID-only account linking is enabled only
  for synthetic QA data; production needs trusted identity proofing, payment reconciliation, AWS
  deployment, and compliance approval.
- Collection intent policy is deterministic and auditable. The Groq knowledge subgraph has scoped
  customer and web-retrieval tools; payment writes remain behind the graph's explicit confirmation
  route and `PaymentsTool`. SQS is at-least-once, so durable end-to-end message de-duplication is
  still required before production use.
