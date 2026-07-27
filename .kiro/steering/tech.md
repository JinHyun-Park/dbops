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

- **IaC**: AWS CDK (Python): ALL infrastructure changes go through CDK, never direct AWS CLI
- **Stacks**: Foundation → Data → Agent → Frontend (dependency chain)
- **Config**: `cdk/config/settings.py` for all environment-specific values

## Data Layer

- **Hot Cache**: Aurora PostgreSQL Serverless v2 (I/O-Optimized)
- **Session/Approval State**: DynamoDB
- **Archive**: S3 + S3 Tables (Apache Iceberg)
- **Vector Store**: pgvector on the Aurora PG cache + Amazon Titan embeddings, the current backend for semantic incident search. S3 Vectors (via Bedrock Knowledge Bases) is planned for future customer runbook/best-practice RAG, not yet implemented.

## MCP Servers

Four MCP servers deployed as Lambda functions behind AgentCore Gateway:

- Performance (11 tools), Incident (9), Operations (34), Simulation (9)
- Total: 63 gateway tools, managed via Gateway Semantic Search
- Of those 63, `get_schema_diff` and `get_schema_history` do NOT work: they query `schema_snapshots`, which no collector writes (there is no `data-pipeline/schema_tracker/`). `diagnose_root_cause` also drops its DDL-change signal for the same reason. Treat 63 as the advertised contract, not the working count.
- Plus 2 agent-local AWS-docs tools (SigV4 proxy to the AWS-managed docs MCP), not a gateway server
- Tool schemas live in `cdk/tool_definitions.py`. That file is the only tool contract the agent sees; `mcp-servers/schemas/*.json` is stale documentation read by nothing.

## Target Engines

Five engine families, classified by `engine_family()` and gated by the `CAPABILITIES` map in `mcp-servers/mcp_servers/shared/engine_family.py`:

- `relational`: Aurora MySQL / Aurora PostgreSQL. SQL via RDS Data API, Performance Insights, full simulator.
- `rds_instance`: standalone (non-Aurora) RDS MySQL and RDS SQL Server. SQL via direct TCP (pymysql for MySQL, pytds for SQL Server, both behind a Data-API-shape adapter in `shared/mysql_direct.py` and `shared/mssql_direct.py`), Performance Insights, right-sizing simulation. No Data API, no Aurora cluster concepts (custom endpoints, prewarm, reader scale-out, cluster parameter groups).
- `documentdb`: Amazon DocumentDB. Mongo wire protocol for deep reads and index writes; control-plane writes via boto3.
- `dynamodb`: Amazon DynamoDB tables. CloudWatch metrics plus control-plane capacity/TTL/PITR writes.
- `elasticache`: ElastiCache Redis / Valkey / Memcached. Live describe plus control-plane writes.

Engine-specific behaviour is data-driven off `CAPABILITIES`, never hardcoded per tool. Handlers return `status: "unsupported_engine"` for a tool that does not apply to the resolved family.

## Key Constraints

- All DB write operations require human approval, enforced by the tool-level `approval_guard` (Cedar Policy Engine is LOG_ONLY defense-in-depth)
- Cross-account access via IAM Hub-Spoke Role Chaining
- RDS Data API for Aurora (`relational`) SQL connectivity; `rds_instance` uses in-VPC direct TCP instead (`sql_via` is the dispatch key)
- Agent queries must include `/* source=dbops-agent */` SQL comment for audit
