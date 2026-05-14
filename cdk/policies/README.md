# AgentCore Cedar Policies

Cedar policies define security rules enforced at the AgentCore Gateway level.
They are applied outside agent code — the agent cannot bypass them.

## Policy Files

- `cedar/performance_policy.cedar` — READ-ONLY for all performance tools
- `cedar/incident_policy.cedar` — READ-ONLY for all incident tools
- `cedar/operations_policy.cedar` — MIXED: read auto, write requires approval

## How Policies Are Applied

1. Policies are uploaded to AgentCore Policy Engine via `agentcore` CLI or API
2. Gateway evaluates policies before executing any tool call
3. Default posture is DENY — only explicitly permitted actions are allowed
4. `forbid` rules override `permit` rules

## Key Rules

- All performance and incident tools are read-only — always permitted
- Operations write tools (execute_sql DDL/DML, modify_parameter, modify_scaling, manage_maintenance) require `approved=true` in the request
- DROP and TRUNCATE SQL are blocked unless `force=true` is explicitly set
- The agent creates approval requests in DynamoDB; DBA approves via UI; agent retries with `approved=true`

## Deployment

```bash
# After AgentCore Gateway is deployed:
agentcore policy create --name dbops-performance --file cedar/performance_policy.cedar
agentcore policy create --name dbops-incident --file cedar/incident_policy.cedar
agentcore policy create --name dbops-operations --file cedar/operations_policy.cedar
```
