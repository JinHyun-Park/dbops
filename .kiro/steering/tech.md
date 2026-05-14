---
inclusion: always
---

# Technology Stack

## Agent Layer

- **Framework**: Strands Agents SDK (Python 3.10+)
- **Runtime**: Amazon Bedrock AgentCore Runtime (single instance, SSE streaming)
- **Tool Integration**: AgentCore Gateway (MCP Protocol) with Cedar Policy Engine
- **LLM**: Amazon Bedrock Claude (default), model-swappable via Strands SDK
- **Memory**: AgentCore Memory (semantic + preference + summary)
- **Knowledge**: Bedrock Knowledge Bases + S3 Vectors backend

## Frontend

- **Framework**: Next.js 15 (App Router, Static Export)
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
- **Vector Store**: S3 Vectors (via Bedrock Knowledge Bases)

## MCP Servers

Five MCP servers deployed as Lambda functions behind AgentCore Gateway:

- Performance (15 tools), Incident (8), Operations (11), Simulation (6), Knowledge (2)
- Total: 42 tools, managed via Gateway Semantic Search

## Key Constraints

- All DB write operations require human approval (Cedar Policy enforced)
- Cross-account access via IAM Hub-Spoke Role Chaining
- RDS Data API for initial cross-account SQL connectivity
- Agent queries must include `/* source=dbops-agent */` SQL comment for audit
