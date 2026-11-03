# Assessment compliance map

| Assessment requirement | Implementation/evidence | Status |
|---|---|---|
| WhatsApp customer channel | `channels/whatsapp.py` is the production boundary; README explicitly states Telegram is only a demo substitute | Prepared adapter; no live credentials |
| 100% AWS architecture | `docs/architecture.md` uses API Gateway, Lambda, Bedrock, DynamoDB/Aurora, SQS, EventBridge, Step Functions, Secrets Manager, CloudWatch, IAM | Design |
| Agentic AI, not only a decision tree | `langchain-agent/` combines explicit LangGraph policy routes with a Groq `create_agent` knowledge subgraph and scoped SQLite/web retrieval tools | Local graph/runtime and tool-calling agent over mock data; WhatsApp adapter is local-only |
| Obtain customer/context data | SQLite repository models clients and external channel identity; production design maps to CRM/billing | Mocked locally |
| Read/write existing systems | `BillingTool`, `PaymentsTool`, and `EscalationTool` isolate read/write boundaries | Mocked with SQLite |
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
| No vector memory as financial authority | Chroma contains public web-page chunks; optional Qdrant memory is client-scoped and excludes financial facts; balances come from SQLite tools | Implemented locally |
| Simple executable README | Root README includes setup and runtime demo/test commands | Documented |

## Explicit simulations and limitations

- Client balances, identities, due dates, and billing outage (`CERR`) are local mock data.
- Telegram polling is optional and requires a bot token; it is not WhatsApp production
  integration.
- Telegram and Chainlit share the runtime contract locally. The Telegram adapter needs a trusted
  external-ID-to-client mapping before it can access account tools.
- The local WhatsApp webhook supports the Meta GET handshake, text messages, and optional Cloud API
  replies through ngrok. It follows AVA for this local test and does not validate POST signatures;
  production signature verification, CRM identity proofing, payment gateway reconciliation, AWS
  deployment, production model integration, and compliance approvals remain unconnected.
- Collection intent policy is deterministic and auditable. The Groq knowledge subgraph has scoped
  customer and web-retrieval tools; payment writes remain behind the graph's explicit confirmation
  route and `PaymentsTool`.
