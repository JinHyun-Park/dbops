---
inclusion: always
---
# Project Structure

```
dbops/
├── cdk/                          # IaC (CDK Python)
│   ├── app.py                    # CDK App entry point
│   ├── config/
│   │   ├── settings.py           # Environment config (edit this for deployment)
│   │   └── settings.example.py   # Template for new deployments
│   └── stacks/
│       ├── foundation_stack.py   # Cognito, VPC, IAM, DynamoDB
│       ├── data_stack.py         # Aurora PG Cache, S3, Bedrock KB, Collectors
│       ├── agent_stack.py        # AgentCore Runtime, Gateway, MCP Lambdas, API GW
│       └── frontend_stack.py     # S3 + CloudFront
├── agent/                        # AgentCore Runtime (Docker container)
│   ├── server.py                 # Strands Agent entry point
│   ├── prompts/                  # System prompt + cheatsheet
│   ├── tools/                    # Agent-local tools (classifier, etc.)
│   └── Dockerfile
├── mcp-servers/                  # MCP Servers (each is a Lambda function)
│   ├── performance/              # 15 tools: query analysis, metrics, monitoring
│   ├── incident/                 # 8 tools: RCA, signal correlation, logs
│   ├── operations/               # 11 tools: params, schema, backup, SQL review
│   ├── simulation/               # 6 tools: upgrade, scaling, DDL impact
│   └── shared/                   # Common utilities (DB connector, cache client)
├── data-pipeline/                # Data Collection Lambdas
│   ├── etl_collector/            # Periodic metrics (1min/5min/1hr)
│   ├── event_processor/          # Real-time RDS/CW events
│   └── schema_tracker/           # Schema snapshot collector
├── api/                          # REST API Lambdas (dashboard, clusters, approvals)
├── frontend/                     # Next.js 15 Web UI
│   └── src/
│       ├── app/                  # App Router pages
│       ├── components/           # design-system/, chat/, dashboard/, query-lab/, approval/
│       ├── lib/                  # agentcore-sse.ts, api-client.ts, auth.ts
│       └── styles/               # design-tokens.css
├── knowledge/                    # Bedrock KB source documents
│   ├── aurora-docs/
│   ├── runbooks/
│   └── best-practices/
├── tests/                        # unit/, integration/, e2e/
├── .kiro/                        # Kiro specs and steering
├── CLAUDE.md                     # Claude Code project instructions
├── AGENTS.md                     # Cross-IDE agent guide
└── README.md
```

## Naming Conventions
- Python: snake_case for files and functions, PascalCase for classes
- TypeScript: camelCase for files and functions, PascalCase for components
- CDK stacks: PascalCase (e.g., `FoundationStack`)
- MCP tools: snake_case (e.g., `get_top_queries`)
- Lambda handlers: `handler.py` in each directory

## Architecture Rules
- All infrastructure changes MUST go through CDK. Never modify AWS resources directly.
- Each MCP server is an independent Lambda with its own `handler.py` and `tools/` directory.
- Shared code lives in `mcp-servers/shared/` and is packaged as a Lambda layer.
- Frontend is a static export (no SSR server needed).
- Environment-specific values ONLY in `cdk/config/settings.py`.
