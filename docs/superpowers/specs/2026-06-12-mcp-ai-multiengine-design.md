# MCP Tools + AI for new engines — Design Spec (program spec #4)

- **Date**: 2026-06-12
- **Status**: Proposed
- **Depends on**: #1/#2/#3 (deployed). ADR 2026-06-12: **Option A** — cache-read first-party
  tools; NO AWS MCP, NO direct DB connection. Writes for new engines stay out (Cedar/approval
  for DocDB/DynamoDB mutations is a later spec).

## Goal

Let the AI chat **diagnose** DocumentDB and DynamoDB resources. Today the agent is Aurora-only;
for non-relational resources `execute_sql` already returns `unsupported_engine` (Phase-1 guard),
but the agent has no way to READ the new-engine findings/metrics. #4 closes that: read-only
cache-based MCP tools + an engine-family-aware agent prompt.

## Architecture (extend the existing read-only incident MCP server — no new Lambda)

- **New tool `get_maintenance_findings(cluster_id)`** in the incident MCP server
  (`mcp-servers/mcp_servers/incident/`): returns the latest `cluster_health_findings` rows
  (check_type/severity/subject/value_str/recommendation/details) via `CacheClient.execute`.
  Engine-agnostic — works for pg/ddb/docdb findings. THIS is the tool that unlocks chat
  diagnosis for the new engines (the findings ARE the diagnosis).
- **Engine-aware `get_health_status`**: for documentdb/dynamodb, include `cluster_meta.engine`
  - `resource_details` (billing mode, capacity context, GSI/LSI for DDB; instances for DocDB)
    so the agent has structural context, not just Aurora-shaped fields.
- **Gateway schema**: add the new tool to `cdk/tool_definitions.py:incident_schema()` so it's
  discoverable (and tools/list pagination already loads all tools).
- **Cedar**: incident tools are READ-ONLY (existing `incident_policy.cedar`) — the new tool is a
  read, covered; no approval surface added.
- **Agent prompt** (`agent/prompts/system_prompt.py` + `cheatsheet.py`): make it
  engine-family-aware — the platform manages Aurora MySQL/PG **and DocumentDB and DynamoDB**.
  For DocDB/DynamoDB: diagnose via `get_maintenance_findings` + `get_health_status` (cache; no
  SQL); writes/`execute_sql` are NOT supported for these engines yet (say so plainly). Add a
  brief DocDB/DynamoDB cheatsheet (DynamoDB: RCU/WCU, throttling, hot partition, GSI; DocDB:
  connections vs limit, replica lag, cursor timeouts, buffer cache hit).
  - **agent/ constraint**: validate prompt edits with `ast.parse` ONLY — never run python in
    `agent/` (a `__pycache__` there makes the Runtime deploy reject the image).

## Testing

- Unit: `get_maintenance_findings` returns rows for a cluster_id (mock CacheClient); engine-aware
  `get_health_status` includes resource_details for non-relational. tool present in incident_schema.
- Live: deploy (incident MCP Lambda + Gateway schema + Runtime prompt). Chat about the DynamoDB
  scenario table (`ddb-...scenario`) → the agent calls `get_maintenance_findings` → explains the
  throttling/underprovisioned findings + recommends. Chat about `dbops-docdb-test` → reads
  findings (healthy) + structural context. Confirm `execute_sql` still refuses for these engines.

## Out of scope (later)

- WRITE/remediation tools for DocDB/DynamoDB (capacity change, TTL, index ops) + their Cedar
  policies + approval binding — a dedicated follow-up (mirrors the operations server's approval flow).
- Mongo-protocol deep diagnosis (#2 follow-up).
