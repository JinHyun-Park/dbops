# DBOps: AI-Powered Database Operations Platform

## Project Overview

AI-powered DBOps platform for DBAs managing AWS database fleets. Built on AWS Bedrock AgentCore + Strands SDK.

Five engine families are supported, defined and capability-gated in `mcp-servers/mcp_servers/shared/engine_family.py`:

| Family         | Covers                                            | SQL path                     |
| -------------- | ------------------------------------------------- | ---------------------------- |
| `relational`   | Aurora MySQL, Aurora PostgreSQL                   | RDS Data API                 |
| `rds_instance` | Standalone (non-Aurora) RDS MySQL, RDS SQL Server | Direct TCP (pymysql / pytds) |
| `documentdb`   | Amazon DocumentDB                                 | none (Mongo wire protocol)   |
| `dynamodb`     | Amazon DynamoDB tables                            | none (control plane)         |
| `elasticache`  | ElastiCache Redis / Valkey / Memcached            | none (control plane)         |

This is not Aurora-only. Registering a DynamoDB table, a DocumentDB cluster, an ElastiCache replication group, or a standalone RDS instance is supported today. Aurora is simply the deepest family. `agent/prompts/system_prompt.py` documents the per-family tool routing the agent follows.

## Architecture Summary

- **Single Agent + Gateway**: One AgentCore Runtime, one Gateway with 4 MCP Servers (63 tools total), plus 2 agent-local AWS-docs tools
- **2-Path Communication**: SSE Direct (AI chat) + REST API (dashboard/data)
- **CDK-First**: All infrastructure via CDK. Never modify AWS resources directly.

## Key Rules

### CDK-Only Infrastructure

All infrastructure changes MUST be made through CDK stacks in `cdk/stacks/`. Never use AWS CLI, Console, or CloudFormation directly. Environment-specific values go in `cdk/config/settings.py` (gitignored real config; never overwrite it, copy from `settings.example.py` on a fresh clone).

### Safety

- All DB read operations are automatic
- All DB write/change operations require human approval, enforced server-side by the tool-level `approval_guard` (fail-closed, payload-hash-bound, single-use). The Cedar Policy Engine is bound at the Gateway in LOG_ONLY mode: defense-in-depth, not the enforcement point (ENFORCE flip deferred pending AgentCore decision-log observability)
- All agent SQL queries must include `/* source=dbops-agent */` comment
- `execute_sql` SQL classification blocks DROP/TRUNCATE without an explicit force flag

### Code Organization

- MCP Server tools: `mcp-servers/mcp_servers/{server}/tools/`, one function per tool. `mcp_servers/` (underscore) is the Python import root: `agent_stack.py` packages `../mcp-servers` as the asset with handler `mcp_servers.<server>.handler.lambda_handler`. A tool written at `mcp-servers/{server}/tools/` ships inside the asset but is NOT importable, so it goes silently dark.
- Shared utilities: `mcp-servers/mcp_servers/shared/`, DB connectors, cache client, `approval_guard`, `engine_family`, `metric_filters`, pricing helpers
- Gateway tool schemas: `cdk/tool_definitions.py`. A new tool that is not registered there is invisible to the agent, no matter how correct the handler is. `mcp-servers/schemas/*.json` is stale documentation read by nothing.
- Agent prompts: `agent/prompts/`, system prompt and cheatsheet separated. Do not run `python`/`py_compile` inside `agent/`: a `__pycache__` directory makes the AgentCore Runtime deploy reject the image.
- Frontend components: `frontend/src/components/{domain}/`
- Design tokens: `frontend/src/app/globals.css` (`:root` custom properties), documented in `DESIGN.md`. There is no `src/styles/` directory.

### Data Flow

- Dashboard/metrics: REST API → Lambda → Aurora PG Cache (pre-collected data)
- AI chat: Browser → AgentCore Runtime SSE → Gateway → MCP Server → Aurora PG Cache or Target DB
- Never call AWS APIs in real-time for dashboard rendering

### Testing

- `python3 -m pytest tests/unit -q` is the main suite (pure-Python, no AWS credentials needed)
- `python3 -m pytest tests/cdk -q` synth-checks all four stacks
- Unit tests for every MCP tool function
- Parity tests you will trip if you add a tool or a shared-file copy: `tests/unit/test_tool_schema_parity.py` (handler signature vs `cdk/tool_definitions.py` vs Cedar allowlists), `tests/unit/test_engine_family.py` (4 `engine_family.py` copies), `tests/unit/test_metric_filters.py`, `tests/unit/test_openapi_spec.py` (regenerate with `python tools/openapi_gen.py`)
- Frontend UI smoke: `cd frontend && npm run e2e` (Playwright, needs `frontend/.env.e2e`)
- There is no `tests/integration/` directory today

## Specs

- Main design spec: `docs/superpowers/specs/2026-05-08-dbops-design.md`
- Kiro specs: `.kiro/specs/phase{N}-*/` (requirements.md, design.md, tasks.md)
- Kiro steering: `.kiro/steering/` (product.md, tech.md, structure.md)
