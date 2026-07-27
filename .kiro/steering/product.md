---
inclusion: always
---

# Product Context

## Purpose

AI-powered DBOps platform for DBAs managing AWS database fleets. Provides natural-language conversational interface for performance analysis, incident diagnosis, operational automation, and upgrade simulation.

## Supported Engines

Five engine families, defined in `mcp-servers/mcp_servers/shared/engine_family.py` (the single source of truth for per-family capabilities):

| Family         | Covers                                            | Depth today                                                                      |
| -------------- | ------------------------------------------------- | -------------------------------------------------------------------------------- |
| `relational`   | Aurora MySQL, Aurora PostgreSQL                   | Deepest: SQL deep-read, Performance Insights, findings, full simulator, writes   |
| `rds_instance` | Standalone (non-Aurora) RDS MySQL, RDS SQL Server | SQL deep-read over direct TCP, findings, approval-gated writes, right-sizing sim |
| `documentdb`   | Amazon DocumentDB                                 | Mongo-protocol deep-read, findings, approval-gated writes                        |
| `dynamodb`     | Amazon DynamoDB tables                            | CloudWatch metrics, findings, capacity/TTL/PITR writes, capacity cost sim        |
| `elasticache`  | ElastiCache Redis / Valkey / Memcached            | Live describe, findings, approval-gated writes, node-resize cost sim             |

Capability gating is data-driven off that `CAPABILITIES` map, and the MCP handlers return `status: "unsupported_engine"` when a tool does not apply. Non-relational families have no SQL surface (`sql: False`). `relational` reaches SQL through the RDS Data API, `rds_instance` through direct TCP (`sql_via`). The Aurora simulator (`simulate_scaling`, upgrade simulation) is relational-only; `rds_instance` uses `simulate_rds_instance_rightsizing` instead.

## Target Users

- DBAs operating Aurora, standalone RDS, DocumentDB, DynamoDB, and ElastiCache across multiple AWS accounts
- Operations teams managing fleet-level database infrastructure

## Business Context

- Designed as a production-grade tool, not a demo
- Intended to be superior to existing SaaS tools (pganalyze, Datadog DB, Percona PMM, Bytebase)
- Distributed as a self-service deployable project via `cdk deploy`
- Users deploy to their own AWS accounts using Claude Code or Kiro

## Core Capabilities

1. **Performance Analysis**: slow query analysis, EXPLAIN interpretation, index recommendations, anomaly detection
2. **Incident Diagnosis**: root cause analysis, signal correlation, timeline reconstruction
3. **Operations Automation**: parameter tuning, schema management, backup, with human-in-the-loop approval
4. **Simulation**: version upgrade impact, parameter change simulation, Aurora scaling cost analysis, DDL impact estimation, DynamoDB capacity cost, ElastiCache node resize, RDS instance right-sizing
5. **Monitoring Dashboard**: real-time metrics from pre-collected cache, not live API calls

## Safety Model

All read operations are automatic. All write/change operations require explicit DBA approval via the Approval Center UI, enforced server-side by the tool-level `approval_guard` (fail-closed, payload-hash-bound, single-use consume). The Cedar Policy Engine is bound at the AgentCore Gateway in LOG_ONLY mode: it evaluates and logs every decision as defense-in-depth, but is not the enforcement point today (flipping to ENFORCE is deferred pending AgentCore decision-log observability).
