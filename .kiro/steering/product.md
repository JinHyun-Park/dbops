---
inclusion: always
---

# Product Context

## Purpose

AI-powered DBOps platform for DBAs managing Amazon Aurora MySQL/PostgreSQL clusters. Provides natural-language conversational interface for performance analysis, incident diagnosis, operational automation, and upgrade simulation.

## Target Users

- DBAs operating Aurora MySQL/PostgreSQL across multiple AWS accounts
- Operations teams managing fleet-level database infrastructure

## Business Context

- Designed as a production-grade tool, not a demo
- Intended to be superior to existing SaaS tools (pganalyze, Datadog DB, Percona PMM, Bytebase)
- Distributed as a self-service deployable project via `cdk deploy`
- Users deploy to their own AWS accounts using Claude Code or Kiro

## Core Capabilities

1. **Performance Analysis** — slow query analysis, EXPLAIN interpretation, index recommendations, anomaly detection
2. **Incident Diagnosis** — root cause analysis, signal correlation, timeline reconstruction
3. **Operations Automation** — parameter tuning, schema management, backup, with human-in-the-loop approval
4. **Simulation** — version upgrade impact, parameter change simulation, scaling cost analysis, DDL impact estimation
5. **Monitoring Dashboard** — real-time metrics from pre-collected cache, not live API calls

## Safety Model

All read operations are automatic. All write/change operations require explicit DBA approval via the Approval Center UI, enforced server-side by the tool-level `approval_guard` (fail-closed, payload-hash-bound, single-use consume). The Cedar Policy Engine is bound at the AgentCore Gateway in LOG_ONLY mode — it evaluates and logs every decision as defense-in-depth, but is not the enforcement point today (flipping to ENFORCE is deferred pending AgentCore decision-log observability).
