---
inclusion: always
---

# Technology Stack

## Agent Layer

- **Framework**: Strands Agents SDK (Python 3.10+)
- **Runtime**: Amazon Bedrock AgentCore Runtime (single instance, SSE streaming)
- **Tool Integration**: AgentCore Gateway (MCP Protocol) with a Cedar Policy Engine bound in LOG_ONLY (write enforcement lives in the tool-level `approval_guard`; Cedar is defense-in-depth)
- **LLM**: Amazon Bedrock Claude (default), model-swappable via Strands SDK
- **Memory**: AgentCore Memory (semantic + preference + summary)
- **Knowledge**: semantic search runs today on pgvector + Amazon Titan embeddings (see Data Layer). Bedrock Knowledge Bases + S3 Vectors are planned (not yet wired) for future RAG over customer-owned runbooks / best-practices; AWS/Aurora public docs are already covered by the agent-local AWS-docs tools.

## Frontend

- **Framework**: Next.js 16 (App Router, Static Export)
- **UI**: shadcn/ui + Tailwind CSS with custom design system
- **Charts**: Recharts or Tremor
- **State**: TanStack Query
- **Auth**: Cognito Hosted UI + Amplify Auth
- **Hosting**: CloudFront + S3

## Infrastructure

- **IaC**: AWS CDK (Python) — ALL infrastructure changes go through CDK, never direct AWS CLI
- **Stacks**: Foundation → Data → Agent → Frontend (dependency chain)
- **Config**: `cdk/config/settings.py` for all environment-specific values

## Data Layer

- **Hot Cache**: Aurora PostgreSQL Serverless v2 (I/O-Optimized)
- **Session/Approval State**: DynamoDB
- **Archive**: S3 + S3 Tables (Apache Iceberg)
- **Vector Store**: pgvector on the Aurora PG cache + Amazon Titan embeddings — the current backend for semantic incident search. S3 Vectors (via Bedrock Knowledge Bases) is planned for future customer runbook/best-practice RAG, not yet implemented.

## MCP Servers

Four MCP servers deployed as Lambda functions behind AgentCore Gateway:

- Performance (11 tools), Incident (9), Operations (34), Simulation (9)
- Total: 63 gateway tools, managed via Gateway Semantic Search
- Plus 2 agent-local AWS-docs tools (SigV4 proxy to the AWS-managed docs MCP) — not a gateway server

## Key Constraints

- All DB write operations require human approval, enforced by the tool-level `approval_guard` (Cedar Policy Engine is LOG_ONLY defense-in-depth)
- Cross-account access via IAM Hub-Spoke Role Chaining
- RDS Data API for initial cross-account SQL connectivity
- Agent queries must include `/* source=dbops-agent */` SQL comment for audit
