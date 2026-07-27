# AI-Powered DBOps Platform — Design Specification

> Version: 1.0
> Date: 2026-05-08
> Status: **SUPERSEDED** (original design record, see the notice below)
> Author: AI-assisted design (Claude Opus 4.6)

---

## SUPERSEDED NOTICE (2026-07-27)

**Read this before anything below.** This document is the _original design record_
from 2026-05-08, kept as the historical artifact of the decisions that started the
project. It is **not** a description of the system that shipped, and it must not be
used as an implementation reference. Several of its most load-bearing specifics
(tool inventory, write enforcement, knowledge path, MCP topology) were changed
during build.

### Where the as-built truth lives

| For                                                   | Read                                                                                 |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Current architecture, stack, safety model             | `.kiro/steering/product.md`, `.kiro/steering/tech.md`, `.kiro/steering/structure.md` |
| Exact gateway tool inventory (names, counts, schemas) | `cdk/tool_definitions.py`, the deployed source of truth                              |
| Per-engine capability depth and the honest gap list   | `docs/superpowers/specs/2026-07-24-engine-parity-audit.md`                           |
| Every change since this document                      | the dated specs in this directory (`2026-06-*`, `2026-07-*`)                         |

### Claims below that no longer hold

Each was re-verified against the code on 2026-07-27.

1. **Cedar does not enforce writes** (affects sections 2.3, 6.1, 6.2, 6.3). Cedar is
   bound at the Gateway in **LOG_ONLY** mode (`cdk/stacks/agent_stack.py`,
   `cedar_mode = "LOG_ONLY"`), and the only live statement per target is a coarse
   permit (`cdk/policies/cedar/*.cedar`). The `context.input.approved == true` rule
   shown in section 6.2 exists only as a commented STEP-2 sketch, never as a deployed
   policy. Write enforcement is the tool-level `approval_guard`
   (`mcp-servers/mcp_servers/shared/approval_guard.py`): fail-closed when
   `APPROVALS_TABLE` is unset, bound to a SHA-256 hash of the approved payload, and
   consumed atomically so an approval is single-use. Cedar is defense in depth.
2. **63 gateway tools, not 30** (affects sections 2.3, 5.1 through 5.5, 11). The
   deployed split is performance 11, incident 9, operations 34, simulation 9. Count
   them in `cdk/tool_definitions.py`. Separately, the agent carries 2 agent-local
   AWS-docs tools (`search_aws_documentation`, `read_aws_documentation` in
   `agent/server.py`) that are Strands `@tool` functions on the Runtime, not gateway
   tools.
3. **No AWS-managed MCP server is a Gateway target** (affects sections 4.2, 5.1, 5.2
   through 5.6, 11, 12). `agent_stack.py` creates exactly 4 `CfnGatewayTarget`s, one
   Lambda each for performance / incident / operations / simulation. No Aurora
   PostgreSQL MCP, Aurora MySQL MCP, CloudWatch MCP, AWS API MCP or AWS Knowledge MCP
   target was ever created. Every capability this document delegates to an official
   AWS MCP was either implemented first-party or dropped.
4. **`explain_plan` is first-party** (affects section 5.2). It lives in
   `mcp-servers/mcp_servers/performance/tools/explain_plan.py` (`explain_plan_impl`)
   and is advertised by `performance_schema()`. Nothing moved to an official Aurora MCP.
5. **The frontend is Next.js 16**, not 15 (affects sections 1.5, 7.1).
   `frontend/package.json` pins `next: 16.2.9`.
6. **Tier-2 knowledge is pgvector plus Titan, not Bedrock KB plus S3 Vectors**
   (affects sections 1.5, 3.1, 4.3, 5.1, 5.6, 12). Semantic incident search runs on
   pgvector columns in the Aurora PG cache with `amazon.titan-embed-text-v2:0`
   embeddings and a cosine (`<=>`) search, falling back to keyword ILIKE
   (`mcp-servers/mcp_servers/incident/tools/similar_incidents.py`). No Bedrock
   Knowledge Base and no S3 Vectors resource exists in any CDK stack, and there is no
   Strands `retrieve` tool on the agent. KB plus S3 Vectors remains a possible future
   path for customer-owned runbook RAG only.
7. **Capacity forecasting never used a `storage_gb` series** (affects section 5.2). No
   collector writes that metric. The real series are `storage_bytes` (VolumeBytesUsed,
   growing) for Aurora and DocumentDB, and `free_storage_bytes` (FreeStorageSpace,
   depleting) for standalone RDS instances, selected per engine family in
   `mcp-servers/mcp_servers/performance/tools/forecast_capacity.py`.
8. **Scope is no longer Aurora MySQL/PostgreSQL only** (affects sections 1.4, 3.1, 3.3).
   `engine_family.py` defines 5 families: `relational` (Aurora), `documentdb`,
   `dynamodb`, `elasticache`, `rds_instance` (standalone RDS MySQL and SQL Server),
   each with its own capability gate. Read the engine parity audit for what depth each
   family actually has.

Sections below carry inline `SUPERSEDED` notes at the specific claims. Everything
un-annotated is either still accurate or historically interesting only. Verify against
the code before relying on any of it.

---

## 1. Overview

### 1.1 Purpose

DBA를 위한 AI 기반 종합 데이터베이스 운영 플랫폼. 자연어 대화로 Aurora MySQL/PostgreSQL 클러스터의 성능 분석, 장애 진단, 운영 자동화, 시뮬레이션을 수행한다.

### 1.2 Goals

- 기존 SaaS(pganalyze, Datadog DB, PMM, Bytebase) 상위 호환 수준의 기능 제공
- Human-in-the-loop 기반 안전한 DB 운영 자동화
- Cross-account 멀티 클러스터 통합 관리
- CDK 기반 self-service 배포: Claude Code 또는 Kiro 사용자가 자신의 AWS 계정에 즉시 배포 가능

### 1.3 Target Users

- AWS 환경에서 Aurora MySQL/PostgreSQL을 운영하는 DBA
- 다수의 클러스터를 여러 AWS 계정에 걸쳐 관리하는 운영팀

### 1.4 Target Database

> **SUPERSEDED:** shipped scope is 5 engine families, not Aurora only. See
> `engine_family.py` (`relational`, `documentdb`, `dynamodb`, `elasticache`,
> `rds_instance`) and the engine parity audit for per-family depth.

- Amazon Aurora MySQL
- Amazon Aurora PostgreSQL

### 1.5 Tech Stack

> **SUPERSEDED:** Frontend is Next.js **16** (`frontend/package.json` pins
> `next: 16.2.9`). The Knowledge Base row never shipped: there is no Bedrock KB and no
> S3 Vectors resource in any stack. Semantic search is pgvector on the Aurora PG cache
> with Titan embeddings.

| Layer            | Technology                                                           |
| ---------------- | -------------------------------------------------------------------- |
| Agent Framework  | Strands Agents SDK (Python)                                          |
| Agent Runtime    | Amazon Bedrock AgentCore Runtime                                     |
| Tool Integration | AgentCore Gateway (MCP Protocol)                                     |
| LLM              | Amazon Bedrock Claude (기본), 모델 교체 가능                         |
| Frontend         | Next.js 15 (App Router) + React + shadcn/ui + Tailwind CSS           |
| IaC              | AWS CDK (Python)                                                     |
| Auth             | Amazon Cognito                                                       |
| Data Store       | Aurora PostgreSQL (Cache), DynamoDB, S3, S3 Tables (Iceberg archive) |
| Knowledge Base   | Bedrock Knowledge Bases + S3 Vectors                                 |

---

## 2. Architecture

### 2.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Web UI (Next.js)                               │
│                     CloudFront + S3 (Static SPA)                     │
│  Chat Panel │ Dashboard │ Query Lab │ Clusters │ Approval │ Reports │
└──────┬─────────────────────┬───────────────────────┬────────────────┘
       │                     │                       │
   SSE Direct            REST API              REST + Async
   (Path A)              (Path B)              (Path C)
       │                     │                       │
       ▼                     ▼                       ▼
┌──────────────┐    ┌──────────────┐    ┌────────────────────────┐
│  AgentCore   │    │  API Gateway │    │  API Gateway           │
│  Runtime     │    │  + Lambda    │    │  + Lambda              │
│  (Cognito)   │    │              │    │  + AgentCore Runtime   │
│              │    │              │    │    (비동기 실행)         │
│  AI 대화     │    │  대시보드     │    │  승인 워크플로          │
│  쿼리 분석   │    │  메트릭 조회  │    │  DDL/DML 실행          │
│  장애 진단   │    │  클러스터 목록│    │  파라미터 변경          │
└──────┬───────┘    └──────┬───────┘    └────────────┬───────────┘
       │                   │                         │
       ▼                   ▼                         ▼
┌──────────────┐    ┌──────────────────────────────────────────────┐
│  AgentCore   │    │            Data Store Layer                   │
│  Gateway     │    │  Aurora PG Cache │ DynamoDB │ S3 │ Bedrock KB│
│  (+Cedar     │    └──────────────────────────────────────────────┘
│   Policy)    │              ▲
└──────┬───────┘              │
       │               ┌─────┴──────────┐
       ▼               │ Data Collection│
┌──────────────┐       │ Pipeline       │
│  MCP Servers │───────│ (ETL+Events)   │
│  (Lambda)    │       └────────────────┘
└──────┬───────┘
       ▼
  Target Aurora MySQL/PostgreSQL
  (Same-account + Cross-account)
```

### 2.2 Three-Path Communication

| Path                | Protocol          | 용도                                 | 중간 레이어                      |
| ------------------- | ----------------- | ------------------------------------ | -------------------------------- |
| **A: SSE Direct**   | SSE over HTTP     | AI 대화, 쿼리 분석, 장애 진단        | 없음 (AgentCore Runtime 직접)    |
| **B: REST API**     | HTTP REST         | 대시보드, 메트릭 조회, 클러스터 목록 | API Gateway + Lambda             |
| **C: REST + Async** | HTTP REST → Async | 승인 워크플로, DDL/DML 실행          | API Gateway + Lambda → AgentCore |

Path A에서 브라우저가 AgentCore Runtime에 직접 SSE 연결한다. Cognito JWT로 인증하며 중간에 Lambda/API Gateway를 두지 않는다. 이는 ChatGPT, Claude.ai 등 업계 표준 패턴과 동일하다.

### 2.3 Design Principles

> **SUPERSEDED (items 4 and 5):** the gateway carries **63** tools, not 42
> (`cdk/tool_definitions.py`). Semantic search is still the routing mechanism. And
> "Cedar Policy로 강제" is wrong: Cedar is bound LOG_ONLY, the tool-level
> `approval_guard` is the enforcement point. Principles 1, 2, 3, 6 held.

1. **CDK-First**: 모든 인프라 변경은 CDK를 통해서만 수행. AWS CLI 직접 수정 금지.
2. **Single Agent + Gateway**: Multi-Runtime 호출의 지연/토큰 문제를 피하기 위해 단일 AgentCore Runtime + Gateway MCP 구조 채택.
3. **Shared Data Store**: AI 에이전트와 대시보드가 동일 데이터 저장소(Aurora PG Cache)를 조회. AI 전용 데이터 경로 없음.
4. **Gateway Semantic Search**: 42개 도구 중 질문당 5-10개만 동적 로드하여 tool explosion 방지.
5. **Human-in-the-loop**: 조회는 자동, 변경은 DBA 승인 필수. Cedar Policy로 강제.
6. **Self-Service Deployment**: `cdk deploy`만으로 전체 스택 배포 가능. 환경별 설정은 config 파일로 분리.

---

## 3. Data Layer

### 3.1 Data Store Architecture

> **SUPERSEDED:** the "Bedrock KB + S3 Vectors" row never shipped. RAG-style semantic
> search lives in the Aurora PG cache itself (pgvector `embedding` columns plus Titan
> embeddings). Retention numbers here are the original intent; the shipped
> `metric_snapshots` and `query_stats` purge is 90 days, a `DELETE` at the end of every
> ETL invocation (`data-pipeline/etl_collector/handler.py`), not partition rotation.

| Store                       | 용도                                | 데이터                                                              | 보존             |
| --------------------------- | ----------------------------------- | ------------------------------------------------------------------- | ---------------- |
| **Aurora PG (Cache)**       | Hot tier, 실시간 대시보드 + AI 조회 | 메트릭 스냅샷, 쿼리 통계, 클러스터 메타, 인덱스 사용률, 슬로우 쿼리 | 7일 (1분 해상도) |
| **DynamoDB**                | 세션, 승인, 이벤트                  | 대화 세션, 승인 이력, RDS 이벤트, 알림 이력                         | 90일             |
| **S3**                      | 장기 보관, 아카이브                 | EXPLAIN Plan, 리포트, 집계 데이터                                   | 1년+             |
| **S3 Tables (Iceberg)**     | Cold tier, 장기 분석                | 7일 지난 메트릭 (5분/1시간 집계)                                    | 1년              |
| **Bedrock KB + S3 Vectors** | RAG 지식 베이스                     | Aurora 문서, 런북, best practice                                    | 상시             |

### 3.2 Aurora PG Cache Schema (핵심 테이블)

```sql
-- 클러스터 메타데이터
cluster_meta (
  cluster_id, account_id, region, engine, engine_version,
  instance_class, status, endpoint, reader_endpoint,
  storage_size_gb, max_connections, updated_at
)

-- PI 메트릭 스냅샷
metric_snapshots (
  cluster_id, timestamp, metric_type,
  value, dimensions_json
) PARTITION BY RANGE (timestamp)

-- pg_stat_statements 스냅샷
query_stats (
  cluster_id, snapshot_time, query_hash, query_text,
  calls, total_time_ms, mean_time_ms, rows,
  shared_blks_hit, shared_blks_read
) PARTITION BY RANGE (snapshot_time)

-- 슬로우 쿼리 로그
slow_queries (
  cluster_id, timestamp, query_text, execution_time_ms,
  lock_time_ms, rows_examined, rows_sent, db_name, user
)

-- 인덱스 사용 통계
index_usage (
  cluster_id, snapshot_time, schema_name, table_name,
  index_name, idx_scan, idx_tup_read, idx_tup_fetch,
  size_bytes
)

-- 연결 통계
connection_stats (
  cluster_id, timestamp, total_connections,
  active_connections, idle_connections,
  connections_by_app_json
)

-- 스키마 스냅샷
schema_snapshots (
  cluster_id, snapshot_time, schema_name,
  tables_json, indexes_json, constraints_json,
  diff_from_previous_json
)

-- NOTE: event_history는 DynamoDB에 저장 (섹션 3.1 참조)
-- Aurora PG Cache에는 메트릭/통계 데이터만 보관
```

### 3.3 Data Collection Pipeline

| Collector               | 주기   | 수집 대상                                             | 저장소                                    |
| ----------------------- | ------ | ----------------------------------------------------- | ----------------------------------------- |
| **PI Collector**        | 1분    | AAS, wait events, counter metrics                     | Aurora PG (metric_snapshots)              |
| **Stats Collector**     | 5분    | pg_stat_statements, connection stats, replication lag | Aurora PG (query_stats, connection_stats) |
| **Meta Collector**      | 5분    | 클러스터 메타데이터, 인스턴스 상태                    | Aurora PG (cluster_meta)                  |
| **Structure Collector** | 1시간  | 인덱스 사용률, 테이블 bloat, 스키마 스냅샷, 파라미터  | Aurora PG (index_usage, schema_snapshots) |
| **Event Processor**     | 실시간 | RDS Events, CloudWatch Alarms, Aurora 이벤트          | DynamoDB (event_history) + SNS            |
| **Archive Job**         | 1일    | 7일 지난 고해상도 데이터 → 집계                       | S3 Tables (Iceberg)                       |

보존 정책: 1분 해상도 7일 → 5분 집계 90일 → 1시간 집계 1년 → S3 장기보관.

---

## 4. Agent Layer

### 4.1 AgentCore Runtime

단일 AgentCore Runtime에 하나의 Strands Agent를 배포한다.

```python
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from strands_tools import retrieve
from mcp.client.streamable_http import streamablehttp_client

model = BedrockModel(
    model_id="anthropic.claude-sonnet-4-20250514-v1:0",
    region_name="us-west-2"
)

gateway_url = f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"
gateway_client = MCPClient(lambda: streamablehttp_client(gateway_url))

with gateway_client:
    gateway_tools = gateway_client.list_tools_sync()
    agent = Agent(
        model=model,
        system_prompt=build_system_prompt(),
        tools=[retrieve, *gateway_tools],
    )
```

Runtime 설정:

- Auth: `RuntimeAuthorizerConfiguration.using_cognito(user_pool, [pkce_client])`
- Protocol: HTTP (SSE streaming)
- Memory: AgentCore Memory (semantic + preference + summary)

### 4.2 AgentCore Gateway

> **SUPERSEDED:** the Gateway has **4** targets, all Lambda, one per custom MCP server
> (`agent_stack.py`, `mcp_lambdas` dict). There is no fifth target and no external MCP
> server target. The Cedar engine bound here is LOG_ONLY, so "세밀한 권한 제어" describes
> an audit trail, not a control.

Gateway는 5개 MCP Server를 단일 MCP 엔드포인트로 통합한다.

기능:

- **Semantic Tool Search**: `x_amz_bedrock_agentcore_search`로 자연어 기반 도구 검색
- **Cedar Policy Engine**: 도구별/사용자별/입력값별 세밀한 권한 제어
- **MCP Sessions**: 세션 기반 연결로 후속 호출 레이턴시 감소

### 4.3 3-Tier Knowledge Strategy

> **SUPERSEDED:** Tier 1 (system prompt cheatsheet) shipped. Tier 2 did **not** ship as
> Bedrock KB plus S3 Vectors and there is no `retrieve` tool on the agent; the semantic
> path that shipped is pgvector plus Titan inside the Aurora PG cache, reached through
> `find_similar_incidents`. Tier 3 shipped as two agent-local tools proxying the
> AWS-managed docs MCP (`search_aws_documentation`, `read_aws_documentation`), not as a
> Gateway target, and `retrieve_skill` is not wired anywhere.

```
Tier 1: System Prompt 치트시트 (항상 포함, ~2,000 토큰)
  - Aurora 핵심 파라미터 30개 요약
  - 공통 진단 워크플로
  - 위험 작업 판단 기준

Tier 2: Bedrock KB + S3 Vectors (retrieve 도구, ~100ms)
  - Aurora 공식 문서
  - 내부 런북, best practice, 장애 보고서

Tier 3: AWS Knowledge MCP (on-demand, 1-5초)
  - 최신 릴리즈 노트, 신규 기능, What's New, 블로그
  - Well-Architected 가이드
  - Skills (단계별 절차 — 업그레이드, 마이그레이션 등)
  - 리전별 서비스 가용성 정보
  - KB 결과 불충분 시 fallback
```

에이전트 판단 기준: 기본 → Tier 2(retrieve), "최신"/"업데이트" 키워드 또는 KB 결과 부족 → Tier 3(AWS Knowledge MCP). `retrieve_skill`은 Simulation MCP의 업그레이드 계획 생성 시 단계별 절차 참조에 활용.

---

## 5. MCP Servers

### 5.1 Overview

> **SUPERSEDED, the whole hybrid premise.** The Official-AWS half never shipped: no
> awslabs MCP server is a Gateway target, and every capability this section delegates
> to one was implemented first-party instead. The shipped inventory is **63 tools
> across 4 Lambda targets**: performance **11**, incident **9**, operations **34**,
> simulation **9** (`cdk/tool_definitions.py`). The only non-gateway tools are the 2
> agent-local AWS-docs proxies in `agent/server.py`. The Cedar Policy column describes
> LOG_ONLY audit intent, not enforcement. Treat the per-server tool lists in 5.2
> through 5.6 as the original plan, not the API.

MCP Server는 **Custom (자체 구현)** + **Official AWS (awslabs 제공)** 하이브리드로 구성한다.
공식 AWS MCP가 표준 DB/API 작업을 처리하고, Custom MCP는 캐시 기반 분석/시뮬레이션 등 부가가치 기능에 집중한다.

**Custom MCP Servers (자체 구현, Lambda):**

| MCP Server       | 도구 수 | Cedar Policy                       | Gateway Target |
| ---------------- | ------- | ---------------------------------- | -------------- |
| Performance      | 10      | READ-ONLY                          | Lambda         |
| Incident         | 6       | READ-ONLY                          | Lambda         |
| Operations       | 8       | MIXED (read auto / write approval) | Lambda         |
| Simulation       | 6       | READ-ONLY                          | Lambda         |
| **Custom Total** | **30**  |                                    |                |

**Official AWS MCP Servers (awslabs 제공, 유지보수 불필요):**

| MCP Server            | 역할                                              | Gateway Target    |
| --------------------- | ------------------------------------------------- | ----------------- |
| Aurora PostgreSQL MCP | 표준 PG 작업 (스키마 조회, 쿼리 실행, EXPLAIN 등) | MCP Server target |
| Aurora MySQL MCP      | 표준 MySQL 작업                                   | MCP Server target |
| CloudWatch MCP        | 메트릭/알람 직접 조회                             | MCP Server target |
| AWS API MCP           | 범용 AWS API 호출 (RDS, Cost Explorer 등)         | MCP Server target |

**Knowledge (Agent 직접 등록):**

| 도구          | 역할                                                   | 등록 방식                 |
| ------------- | ------------------------------------------------------ | ------------------------- |
| retrieve      | Bedrock KB + S3 Vectors (Tier 2)                       | Strands native            |
| aws_knowledge | AWS Knowledge MCP — 문서, Skills, 리전 가용성 (Tier 3) | Gateway MCP Server target |

### 5.2 Performance MCP Server (10 tools, Custom)

> **SUPERSEDED:** 11 tools shipped, and nothing was handed to an official Aurora MCP.
> `explain_plan` is a first-party tool
> (`mcp-servers/mcp_servers/performance/tools/explain_plan.py`), so the "공식 AWS
> MCP로 이관된 기능" list at the end of this section describes work that either landed
> here or was dropped, not work that moved.
>
> Also on `forecast_capacity`: there is no `storage_gb` metric and never was. The real
> series are `storage_bytes` (VolumeBytesUsed, growing) for Aurora and DocumentDB and
> `free_storage_bytes` (FreeStorageSpace, depleting) for standalone RDS instances,
> picked per engine family in `forecast_capacity.py`.

`explain_query`는 공식 Aurora MCP로 이관. 캐시 기반 분석과 사전 연산에 집중.

**캐시 기반 조회 (4):**

- `get_top_queries` — Aurora PG Cache에서 Top-N 쿼리 (총 시간/호출 수/평균 시간 기준)
- `get_pi_metrics` — Aurora PG Cache에서 PI 메트릭 (AAS, wait events, counter metrics)
- `get_slow_queries` — Aurora PG Cache에서 슬로우 쿼리 목록 (MySQL: slow query log 파싱, PG: pg_stat_statements 기반)
- `compare_periods` — 두 기간의 메트릭 비교 분석

**사전 연산 분석 (4):**

- `detect_anomalies` — 최근 N시간 메트릭을 7일 이동평균 baseline 대비 z-score로 이상 탐지
- `detect_regressions` — 특정 시점 전후 쿼리 성능 비교 (배포 후 느려진 쿼리 탐지)
- `forecast_capacity` — 스토리지/연결 수 선형 회귀 예측 (N일 후 한계 도달 예상)
- `get_performance_summary` — 지정 기간 핵심 KPI 요약 (avg_aas, top_waits, slow_query_count 등)

**캐시 기반 상세 모니터링 (2):**

- `recommend_index` — index_usage + query_stats 조합 분석으로 인덱스 추천
- `get_vacuum_stats` — (PG) autovacuum 현황, dead tuples, bloat ratio

**공식 AWS MCP로 이관된 기능:**

- EXPLAIN ANALYZE → 공식 Aurora PG/MySQL MCP
- Lock/Blocking 분석 → 공식 Aurora PG/MySQL MCP (pg_locks, innodb_lock_waits 조회)
- Connection 분석 → 공식 Aurora PG/MySQL MCP (pg_stat_activity, SHOW PROCESSLIST)
- Replication 상태 → 공식 Aurora PG/MySQL MCP + AWS API MCP (DescribeDBClusters)
- 비용 분석 → AWS API MCP (Cost Explorer API)

### 5.3 Incident MCP Server (6 tools, Custom)

> **SUPERSEDED:** 9 tools shipped. The 6 below all exist, plus `diagnose_root_cause`,
> `get_maintenance_findings` and `get_remediation_history`. Nothing was handed to a
> CloudWatch or Aurora MCP. `find_similar_incidents` searches pgvector in the Aurora PG
> cache, not a Bedrock KB.

`get_alarm_history`와 `get_connections`는 공식 CloudWatch/Aurora MCP로 이관.

- `get_health_status` — 클러스터 건강 상태 종합 (캐시 기반 인스턴스 상태, 연결, 복제 지연)
- `get_recent_events` — DynamoDB event_history에서 RDS Events, 알람, failover 이력
- `search_logs` — CloudWatch Logs Insights로 Aurora error/audit log 검색
- `correlate_signals` — 메트릭 + 이벤트 + 로그를 시간축 정렬하여 장애 타임라인 구성
- `get_incident_summary` — 최근 N일 장애/이벤트 통계 (MTTR, 빈도, 유형별 분류)
- `find_similar_incidents` — Bedrock KB에서 현재 증상과 유사한 과거 장애 사례 검색

**공식 AWS MCP로 이관된 기능:**

- 알람 이력 → 공식 CloudWatch MCP
- 활성 세션 목록 → 공식 Aurora PG/MySQL MCP

### 5.4 Operations MCP Server (8 tools, Custom)

> **SUPERSEDED:** 34 tools shipped, the largest server by far. Beyond the 8 below it
> carries `request_approval`, snapshot/restore, Aurora custom endpoints, reader
> scale-out and prewarm, the DynamoDB / DocumentDB / ElastiCache / standalone-RDS write
> tools, `query_activity_audit` and `get_runbook`. Nothing was handed to an AWS API MCP.
> Every write is gated by the tool-level `approval_guard`, not by Cedar.

`get_parameters`, `get_backup_status`, `get_scaling_info`는 공식 AWS API MCP로 이관.

**캐시 기반 조회 (2, 자동 허용):**

- `get_schema_diff` — 두 환경/시점 간 스키마 비교 (캐시된 schema_snapshots 사용)
- `get_schema_history` — 스키마 변경 이력 추적 (캐시 기반)

**실행 (4, 승인 필요):**

- `execute_sql` — SQL 실행 (SELECT 자동 허용, DDL/DML 승인 필요)
- `modify_parameter` — DB 파라미터 변경
- `modify_scaling` — 인스턴스 스케일링
- `manage_maintenance` — 유지보수 윈도우 관리

**분석 (2, 자동 허용):**

- `review_sql` — DDL/DML 실행 전 자동 리뷰 (위험도, 영향 행 수, 락 시간 추정, 롤백 SQL)
- `audit_permissions` — DB 사용자/역할 권한 감사 (과도한 권한, 미사용 계정 탐지)

**공식 AWS MCP로 이관된 기능:**

- 현재 파라미터 값 조회 → 공식 Aurora PG/MySQL MCP
- 백업 이력/상태 → AWS API MCP (DescribeDBClusterSnapshots)
- 현재 용량/ACU/스토리지 → AWS API MCP (DescribeDBClusters)

### 5.5 Simulation MCP Server (6 tools)

> **SUPERSEDED:** 9 tools shipped. The 6 below plus
> `simulate_dynamodb_capacity_cost`, `simulate_elasticache_node_resize` and
> `simulate_rds_instance_rightsizing`.

- `check_upgrade_compatibility` — 버전 업그레이드 호환성 체크 (deprecated 기능, 새 기능, 호환 SQL)
- `estimate_upgrade_impact` — 업그레이드 방식별 예상 시간/다운타임/리스크 분석
- `generate_upgrade_plan` — 업그레이드 실행 계획서 생성 (체크리스트, 절차, 롤백 계획)
- `simulate_parameter_change` — 파라미터 변경 영향 분석 (static/dynamic, 영향 범위, 연관 파라미터)
- `simulate_scaling` — 스케일 업/다운 비용-성능 트레이드오프 분석
- `simulate_ddl_impact` — DDL 영향도 분석 (테이블 크기, 예상 락 시간, 온라인 DDL 가능 여부)

### 5.6 Knowledge (2 tools)

> **SUPERSEDED:** neither tool shipped as described. There is no `retrieve` tool and no
> Bedrock KB. `aws_knowledge` is not a Gateway target: the AWS docs surface is two
> agent-local Strands tools, `search_aws_documentation` and `read_aws_documentation`,
> which SigV4-proxy the AWS-managed docs MCP from inside the Runtime
> (`agent/server.py`).

- `retrieve` — Bedrock KB + S3 Vectors 검색 (Tier 2). Strands 네이티브 도구로 Agent에 직접 등록.
- `aws_knowledge` — AWS Knowledge MCP Server 조회 (Tier 3). Gateway에 외부 MCP Server 타겟으로 등록. Documentation MCP보다 넓은 범위: 공식 문서 + What's New + 블로그 + Well-Architected + Skills(단계별 절차) + 리전 가용성 포함.

---

## 6. Safety & Policy

### 6.1 5-Layer Safety Model

> **SUPERSEDED, this is the most misleading section in the document.** L3 is not a
> control today: Cedar is bound at the Gateway in LOG_ONLY mode, and the only deployed
> statement per target is a coarse permit. The layer that actually blocks unapproved
> writes is a **sixth** one this design did not have: the tool-level `approval_guard`
> (`mcp-servers/mcp_servers/shared/approval_guard.py`), which is fail-closed,
> payload-hash-bound and single-use. L1, L2, L4, L5 shipped as described, plus
> `execute_sql` SQL classification that blocks DROP/TRUNCATE without an explicit force
> flag.

| Layer                     | 구현                                                 | 역할                         |
| ------------------------- | ---------------------------------------------------- | ---------------------------- |
| **L1: Query Sandbox**     | MCP Server 내 read-only DB 연결 기본                 | 의도치 않은 쓰기 방지        |
| **L2: SQL Audit Trail**   | 모든 에이전트 쿼리에 `/* source=dbops-agent */` 주석 | 추적성                       |
| **L3: Cedar Policy**      | AgentCore Policy Engine                              | 도구/사용자/입력값 레벨 제어 |
| **L4: Human-in-the-loop** | 승인 워크플로 (DynamoDB + Web UI)                    | 변경 작업 DBA 승인           |
| **L5: Dry-run Mode**      | EXPLAIN만 실행, 결과 미리보기                        | 실행 전 영향 확인            |

### 6.2 Cedar Policy Examples

> **SUPERSEDED, none of these policies is deployed.** The live files are
> `cdk/policies/cedar/{performance,incident,operations,simulation}_policy.cedar`, and
> each contains exactly one coarse `permit` over its target. The
> `context.input.approved == true` rule and the DROP/TRUNCATE `forbid` survive only as
> commented STEP-2 sketches inside `operations_policy.cedar`, to be written when the
> binding flips to ENFORCE. Practical gotchas learned since: one statement per policy,
> the action form is `<target>___<tool>` with three underscores, `context.input.<param>`
> must be a real declared tool parameter, and AgentCore Cedar has no `toUpper` /
> `startsWith` / `contains`, so the SQL prefix rule in the first example is not
> expressible and lives in `execute_sql.py` instead. The third example is also unsound
> as written: a pattern match on raw SQL is bypassable (MySQL executable comments
> `/*!...*/` really execute on Aurora MySQL), which is why classification is done in
> Python (`mcp-servers/mcp_servers/shared/sql_safety.py`). Read
> `cdk/policies/README.md` for the current, accurate version of all of this.

```cedar
// Performance MCP: SELECT/EXPLAIN만 허용
permit(
  principal,
  action == Action::"explain_query",
  resource
) when {
  context.input.sql.toUpper().startsWith("SELECT") ||
  context.input.sql.toUpper().startsWith("EXPLAIN")
};

// Operations MCP: approved 플래그 필수
permit(
  principal,
  action in [Action::"modify_parameter", Action::"modify_scaling", Action::"execute_sql"],
  resource
) when {
  context.input.approved == true
};

// 위험 SQL 완전 차단
forbid(
  principal,
  action == Action::"execute_sql",
  resource
) when {
  context.input.sql matches "DROP|TRUNCATE|DELETE\\s+FROM"
  && context.input.force != true
};
```

### 6.3 Human-in-the-loop Approval Flow

> **SUPERSEDED at step 3.** Cedar does not block the call: it logs a decision and lets
> it through. The write tool itself refuses, because the agent must first call
> `request_approval` (which mints a DynamoDB row carrying a hash of the exact payload),
> and on the re-issue the tool calls `verify_approval` with the approval id, cluster id,
> action type and the payload it is about to run. That check confirms the row exists,
> is `approved`, matches the
> cluster and action type, matches the payload hash, is inside the replay window, and
> has not been consumed, then atomically consumes it so it cannot be replayed. Steps 1,
> 2, 4, 5, 6, 7 are otherwise accurate.

```
1. DBA가 변경 요청 (자연어)
2. Agent가 변경 도구 호출 시도
3. Cedar Policy가 approved=false로 차단
4. Agent가 Web UI에 승인 요청 생성 (DynamoDB에 pending 저장)
5. Web UI Approval Center에 요청 표시 (변경 내용, 영향 분석, 위험도)
6. DBA가 승인/거부/수정
7. 승인 시: approved=true로 재호출 → 실행 → 결과 DynamoDB에 감사 로그
```

---

## 7. Web UI

### 7.1 Technology

> **SUPERSEDED:** Next.js **16** (`frontend/package.json` pins `next: 16.2.9`, React
> 19). Everything else in this list shipped.

- Next.js 15 (App Router, Static Export)
- CloudFront + S3 배포
- shadcn/ui + Tailwind CSS (커스텀 디자인 시스템)
- Recharts / Tremor (차트)
- TanStack Query (서버 상태)
- Cognito Hosted UI + Amplify Auth

### 7.2 Design Strategy

**Claude Design → Claude Code 워크플로:**

1. Claude Design(claude.ai)에서 디자인 시스템 + 핵심 페이지 목업 생성
2. Handoff bundle export
3. Claude Code에서 frontend-design 스킬로 구현
4. 브라우저 검증 반복

**디자인 원칙:**

- Dark Mode First (DBA의 터미널 작업 환경에 맞춤)
- Information Density > White Space (한 화면에 필요한 정보 최대 표시)
- Command Palette (Cmd+K) — 모든 기능에 키보드 접근
- Contextual AI Panel — 어디서든 슬라이드 패널로 AI 대화 가능
- 전용 컬러 팔레트 (기본 shadcn 컬러 사용 금지)

**디자인 레퍼런스:** Linear (미니멀 레이아웃), Grafana (대시보드 그리드), pganalyze (DB 전용 UI), Vercel Dashboard (모노톤), Raycast (커맨드 팔레트)

### 7.3 Pages

| Page                | 데이터 소스                | 통신 경로                 |
| ------------------- | -------------------------- | ------------------------- |
| **Chat**            | AgentCore Runtime          | Path A (SSE Direct)       |
| **Dashboard**       | Aurora PG Cache            | Path B (REST API)         |
| **Clusters**        | Aurora PG Cache + DynamoDB | Path B (REST API)         |
| **Query Lab**       | AgentCore Runtime          | Path A (SSE Direct)       |
| **Approval Center** | DynamoDB                   | Path B + C (REST + Async) |
| **Reports**         | S3 + DynamoDB              | Path B (REST API)         |

---

## 8. Cross-Account Architecture

### 8.1 Hub-Spoke IAM Role Chaining

```
Central Account (Hub)          Target Account (Spoke)
┌──────────────────┐           ┌───────────────────────┐
│ MCP Server Lambda│           │ Spoke Role            │
│       │          │           │ dbops-spoke-role      │
│       ▼          │           │                       │
│ Hub Role         │──assume──▶│ Trust: Hub Account    │
│ dbops-hub-role   │  role     │ Permissions:          │
│                  │           │  rds:Describe*        │
│                  │           │  pi:GetResource*      │
│                  │           │  logs:StartQuery      │
│                  │           │  rds:Modify* (조건부) │
└──────────────────┘           └───────────────────────┘
```

### 8.2 Network Connectivity

| Phase     | 방식                       | 용도                                      |
| --------- | -------------------------- | ----------------------------------------- |
| Phase 1-3 | **RDS Data API**           | 네트워크 설정 없이 HTTPS로 SQL 실행       |
| Phase 4+  | **Transit Gateway** (선택) | 직접 TCP 연결 필요 시 (고성능, 다수 계정) |

### 8.3 Cluster Registration

DBA가 Web UI에서 클러스터를 등록하면:

1. Account ID, Region, Cluster ID, Spoke Role ARN 입력
2. 연결 테스트 (Hub → AssumeRole → Spoke → rds:DescribeDBClusters)
3. DynamoDB cluster_registry 테이블에 저장
4. Data Collection Pipeline이 자동으로 해당 클러스터 수집 시작

---

## 9. CDK Infrastructure

### 9.1 Stack Structure

> **SUPERSEDED, minor:** the 4 stacks and their dependency order shipped as designed,
> but `data_stack.py` contains no Bedrock KB (it was never created), and there is no
> `config/clusters.py`. Cluster registration is runtime-only, through DynamoDB.

```
cdk/
├── app.py                     # CDK App entry point
├── config/
│   ├── settings.py            # 환경별 설정 (dev/staging/prod)
│   └── clusters.py            # 초기 클러스터 레지스트리 (선택)
└── stacks/
    ├── foundation_stack.py    # Cognito, VPC, IAM, DynamoDB
    ├── data_stack.py          # Aurora PG, S3, Bedrock KB, Collectors
    ├── agent_stack.py         # AgentCore Runtime, Gateway, MCP Lambdas, API GW
    └── frontend_stack.py      # S3 + CloudFront
```

의존성: Foundation → Data → Agent → Frontend

### 9.2 Self-Service Deployment

이 프로젝트는 다른 사용자가 자신의 AWS 계정에 배포할 수 있도록 설계한다.

**배포 전제 조건:**

- AWS 계정 + AdministratorAccess (또는 동등 권한)
- Node.js 20+, Python 3.10+, AWS CDK CLI
- Bedrock model access 활성화 (Claude Sonnet)

**배포 절차:**

```bash
# 1. 프로젝트 클론
git clone <repo-url>
cd dbops

# 2. 환경 설정
cp cdk/config/settings.example.py cdk/config/settings.py
# settings.py에서 region, account_id, cognito 설정 편집

# 3. 의존성 설치
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..

# 4. CDK 배포
cd cdk
cdk bootstrap
cdk deploy --all

# 5. 첫 번째 클러스터 등록
# Web UI 접속 → Clusters → + 클러스터 등록
```

**Configuration-as-Code 원칙:**

- 모든 환경 차이는 `config/settings.py`에서 관리
- 기능 토글, 리전, 인스턴스 크기 등을 config로 제어
- AWS CLI로 직접 리소스 수정 금지 — 항상 CDK를 통해 변경

### 9.3 Config Structure

```python
# cdk/config/settings.py
class Settings:
    # 환경
    ENV = "prod"  # dev | staging | prod
    REGION = "ap-northeast-2"
    ACCOUNT_ID = "123456789012"

    # Cognito
    COGNITO_DOMAIN_PREFIX = "dbops-prod"
    CALLBACK_URLS = ["https://dbops.example.com/callback"]

    # AgentCore
    AGENT_MODEL_ID = "anthropic.claude-sonnet-4-20250514-v1:0"
    GATEWAY_SEMANTIC_SEARCH = True

    # Data Collection
    PI_COLLECTION_INTERVAL_MIN = 1
    STATS_COLLECTION_INTERVAL_MIN = 5
    STRUCTURE_COLLECTION_INTERVAL_MIN = 60

    # Aurora PG Cache
    CACHE_DB_MIN_ACU = 0.5
    CACHE_DB_MAX_ACU = 4

    # Feature Flags
    ENABLE_SIMULATION = True
    ENABLE_CROSS_ACCOUNT = True
    ENABLE_REPORTS = True
    ENABLE_COST_ANALYSIS = True
```

---

## 10. Project Structure

```
dbops/
├── cdk/                          # IaC (CDK Python)
│   ├── app.py
│   ├── config/
│   │   ├── settings.py
│   │   └── settings.example.py
│   └── stacks/
│       ├── foundation_stack.py
│       ├── data_stack.py
│       ├── agent_stack.py
│       └── frontend_stack.py
├── agent/                        # AgentCore Runtime
│   ├── server.py
│   ├── prompts/
│   │   ├── system_prompt.py
│   │   └── cheatsheet.py
│   ├── tools/
│   └── Dockerfile
├── mcp-servers/                  # MCP Servers (Lambda)
│   ├── performance/
│   │   ├── handler.py
│   │   └── tools/
│   ├── incident/
│   │   ├── handler.py
│   │   └── tools/
│   ├── operations/
│   │   ├── handler.py
│   │   └── tools/
│   ├── simulation/
│   │   ├── handler.py
│   │   └── tools/
│   └── shared/
│       ├── db_connector.py
│       ├── cache_client.py
│       └── policy_helpers.py
├── data-pipeline/                # Data Collection Lambdas
│   ├── etl_collector/
│   ├── event_processor/
│   └── schema_tracker/
├── api/                          # REST API Lambdas
│   ├── dashboard/
│   ├── clusters/
│   ├── approvals/
│   └── reports/
├── frontend/                     # Next.js Web UI
│   ├── src/
│   │   ├── app/
│   │   ├── components/
│   │   │   ├── design-system/
│   │   │   ├── chat/
│   │   │   ├── dashboard/
│   │   │   ├── query-lab/
│   │   │   └── approval/
│   │   ├── lib/
│   │   │   ├── agentcore-sse.ts
│   │   │   ├── api-client.ts
│   │   │   └── auth.ts
│   │   └── styles/
│   │       └── design-tokens.css
│   └── package.json
├── knowledge/                    # Bedrock KB 소스 문서
│   ├── aurora-docs/
│   ├── runbooks/
│   └── best-practices/
├── tests/                        # 테스트
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── docs/
│   └── superpowers/
│       └── specs/
├── CLAUDE.md                     # Claude Code 프로젝트 설정
├── AGENTS.md                     # 에이전트 가이드 (Claude Code + Kiro 공용)
└── README.md
```

---

## 11. Phase Strategy

> **SUPERSEDED, this whole section is a historical plan.** The 5 phases were delivered
> but the tool-count table at the end never matched reality, and development continued
> well past Phase 5 (multi-engine, ElastiCache, standalone RDS instances, tenancy,
> agent tasks, the remediation outcome loop). Current tool counts are performance 11,
> incident 9, operations 34, simulation 9, total 63, plus 2 agent-local AWS-docs tools.
> The "공식 AWS MCP (Phase 1부터 Gateway에 등록)" line at the end is wrong: no AWS-managed
> MCP server was ever registered as a Gateway target. For what actually landed and in
> what order, read the dated specs in this directory.

### Phase 1: Foundation + 핵심 에이전트 (MVP)

**목표:** 하나의 Aurora 클러스터에 대해 AI 대화로 성능 분석

**범위:**

- CDK Foundation + Data Stack (기본)
- AgentCore Runtime + Gateway
- Performance MCP Server (6개 핵심 도구)
- Data Pipeline (ETL Collector 5분 주기)
- Bedrock KB + S3 Vectors
- Frontend: Chat Panel + 기본 Dashboard

**검증:** DBA가 대화로 slow query 분석 → EXPLAIN → 인덱스 추천 가능

**예상 기간:** 3-4주

### Phase 2: 장애 진단 + 분석 강화

**목표:** 장애 발생 시 AI RCA + 분석 리포트 자동 생성

**범위:**

- Incident MCP Server (8개 도구)
- Performance 분석 도구 추가 (+4)
- Event Processor Lambda
- Report Pipeline (Daily/Weekly)
- Frontend: Query Lab + Report 뷰어
- 3-Tier Knowledge 완성

**검증:** 알람 발생 → AI 시그널 상관분석 → RCA 제시

**예상 기간:** 3-4주

### Phase 3: 운영 자동화 + 승인

**목표:** DBA 승인 기반 파라미터 변경, DDL 실행

**범위:**

- Operations MCP Server (11개 도구)
- Cedar Policy 고도화
- Human-in-the-loop 승인 흐름 완성
- Frontend: Approval Center + Cluster 관리
- 감사 로그

**검증:** "파라미터 변경해줘" → 영향 분석 → 승인 → 실행 → 로그

**예상 기간:** 3-4주

### Phase 4: 시뮬레이션 + Cross-Account

**목표:** 버전 업그레이드 시뮬레이션 + 타 계정 DB 통합 관리

**범위:**

- Simulation MCP Server (6개 도구)
- Cross-Account IAM (Hub-Spoke)
- 클러스터 등록/관리 UI
- 멀티 클러스터 대시보드

**검증:** 타 계정 클러스터 등록 → 시뮬레이션 → 업그레이드 계획서

**예상 기간:** 3-4주

### Phase 5: 고도화 + SaaS 완성도

**목표:** 기존 SaaS 상위 호환 수준

**범위:**

- EventBridge proactive 모니터링
- Lock/Blocking, VACUUM, 비용 분석
- S3 Tables 아카이브
- 고급 리포트 (월간 종합, capacity planning)
- Command Palette + 키보드 네비게이션
- Claude Design 기반 UI 완성

**검증:** pganalyze/Datadog DB 기능 커버리지 90%+

**예상 기간:** 4-6주

### Phase 도구 배포 현황

**Custom MCP 도구:**

| Phase | Perf | Incident | Ops | Sim | Knowledge | Custom Total |
| ----- | ---- | -------- | --- | --- | --------- | ------------ |
| 1     | 4    | -        | -   | -   | 2         | 6            |
| 2     | 8    | 6        | -   | -   | 2         | 16           |
| 3     | 8    | 6        | 8   | -   | 2         | 24           |
| 4     | 8    | 6        | 8   | 6   | 2         | 30           |
| 5     | 10   | 6        | 8   | 6   | 2         | 32           |

**공식 AWS MCP (Phase 1부터 Gateway에 등록, 추가 구현 불필요):**

- Aurora PostgreSQL MCP, Aurora MySQL MCP, CloudWatch MCP, AWS API MCP, AWS Knowledge MCP

---

## 12. Research References

### Architecture Decisions

> **SUPERSEDED, 3 rows were reversed in build.** "Vector store: S3 Vectors" and "RAG
> strategy: 3-Tier Hybrid" both lost their Bedrock KB leg: the semantic store that
> shipped is pgvector on the Aurora PG cache with Titan embeddings, which needed no new
> service and no new stack. "MCP tool source: Custom + Official AWS MCP 하이브리드" was
> reversed outright: everything is first-party, and the only AWS-managed surface is the
> docs MCP reached by two agent-local proxy tools. The remaining rows held, including
> the two that mattered most (Single Agent + Gateway, and Aurora PG over Athena for the
> metrics cache).

| Decision              | Chosen                               | Rejected                | Reason                                                                                    |
| --------------------- | ------------------------------------ | ----------------------- | ----------------------------------------------------------------------------------------- |
| Agent topology        | Single Agent + Gateway               | Multi-Agent Runtime     | Multi-Runtime 호출 시 지연/토큰 문제 (이전 AIOps 실전 경험)                               |
| Tool routing          | Gateway Semantic Search              | Custom Haiku Classifier | Gateway 내장 기능으로 별도 구현 불필요                                                    |
| Streaming             | SSE Direct to Runtime                | Lambda proxy            | Lambda proxy 시 SSE 스트리밍 불가                                                         |
| Metrics cache         | Aurora PG                            | S3 Tables + Athena      | Athena 레이턴시 2-10초, 대시보드 <500ms 불가. 비용도 10-50x 높음                          |
| Vector store          | S3 Vectors                           | OpenSearch Serverless   | OpenSearch 최소 $700/월 vs S3 Vectors $5-10/월                                            |
| RAG strategy          | 3-Tier Hybrid                        | Bedrock KB only         | 최신성(AWS Knowledge MCP) + 빠른 응답(치트시트) + 내부 문서(KB) 모두 필요                 |
| MCP tool source       | Custom + Official AWS MCP 하이브리드 | Custom only             | 공식 AWS MCP가 표준 DB/API 작업 처리, Custom은 분석/시뮬레이션에 집중. 유지보수 부담 감소 |
| Knowledge MCP         | AWS Knowledge MCP                    | AWS Documentation MCP   | Knowledge MCP가 Skills, Well-Architected, 블로그, 리전 가용성까지 포함하여 더 넓은 범위   |
| Cross-account network | RDS Data API (초기)                  | VPC Peering             | 네트워크 설정 없이 즉시 시작 가능                                                         |
| UI design             | Claude Design → Claude Code          | Code-only               | AI 생성 느낌 방지, 제품다운 디자인 품질 확보                                              |

### External References

- [AgentCore Documentation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/)
- [Strands Agents SDK](https://strandsagents.com/)
- [AgentCore Gateway Quick Start](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-quick-start.html)
- [Cedar Policy Language](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-understanding-cedar.html)
- [S3 Vectors + Bedrock KB](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html)
- [PlanetScale MCP Server](https://planetscale.com/blog/introducing-planetscale-mcp-server) (안전 패턴 참조)
- [AWS PI Reporter](https://aws.amazon.com/blogs/database/ai-powered-tuning-tools-for-amazon-rds-for-postgresql-and-amazon-aurora-postgresql-databases-pi-reporter/)
- [Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Introducing Claude Design](https://www.anthropic.com/news/claude-design-anthropic-labs)
- [AWS MCP Server GA (May 2026)](https://aws.amazon.com/blogs/aws/the-aws-mcp-server-is-now-generally-available/)
- [AWS Knowledge MCP Server](https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server)
- [AWS MCP Servers Collection (awslabs)](https://awslabs.github.io/mcp/)
- [AWS IaC MCP Server](https://awslabs.github.io/mcp/servers/aws-iac-mcp-server)
