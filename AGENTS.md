# DBOps — AI-Powered Database Operations Platform

## Project Overview
AI-powered DBOps platform for DBAs managing Amazon Aurora MySQL/PostgreSQL. Built on AWS Bedrock AgentCore + Strands SDK.

## Architecture Summary
- **Single Agent + Gateway**: One AgentCore Runtime, one Gateway with 5 MCP Servers (42 tools total)
- **2-Path Communication**: SSE Direct (AI chat) + REST API (dashboard/data)
- **CDK-First**: All infrastructure via CDK. Never modify AWS resources directly.

## Key Rules

### CDK-Only Infrastructure
All infrastructure changes MUST be made through CDK stacks in `cdk/stacks/`. Never use AWS CLI, Console, or CloudFormation directly. Environment-specific values go in `cdk/config/settings.py`.

### Safety
- All DB read operations are automatic
- All DB write/change operations require human approval (Cedar Policy enforced)
- All agent SQL queries must include `/* source=dbops-agent */` comment
- Cedar Policy blocks DROP/TRUNCATE without explicit force flag

### Code Organization
- MCP Server tools: `mcp-servers/{server}/tools/` — one function per tool
- Shared utilities: `mcp-servers/shared/` — DB connector, cache client, policy helpers
- Agent prompts: `agent/prompts/` — system prompt and cheatsheet separated
- Frontend components: `frontend/src/components/{domain}/`
- Design tokens: `frontend/src/styles/design-tokens.css`

### Data Flow
- Dashboard/metrics: REST API → Lambda → Aurora PG Cache (pre-collected data)
- AI chat: Browser → AgentCore Runtime SSE → Gateway → MCP Server → Aurora PG Cache or Target DB
- Never call AWS APIs in real-time for dashboard rendering

### Testing
- Unit tests for every MCP tool function
- CDK snapshot tests for all stacks
- Integration tests against Aurora PG Cache
- E2E tests for chat flow

## Specs
- Main design spec: `docs/superpowers/specs/2026-05-08-dbops-design.md`
- Kiro specs: `.kiro/specs/phase{N}-*/` (requirements.md, design.md, tasks.md)
- Kiro steering: `.kiro/steering/` (product.md, tech.md, structure.md)
