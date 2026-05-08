# AI-Powered DBOps Platform — Design Specification

> Version: 1.0
> Date: 2026-05-08
> Status: Draft
> Author: AI-assisted design (Claude Opus 4.6)

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

- Amazon Aurora MySQL
- Amazon Aurora PostgreSQL

### 1.5 Tech Stack

| Layer | Technology |
|---|---|
| Agent Framework | Strands Agents SDK (Python) |
| Agent Runtime | Amazon Bedrock AgentCore Runtime |
| Tool Integration | AgentCore Gateway (MCP Protocol) |
| LLM | Amazon Bedrock Claude (기본), 모델 교체 가능 |
| Frontend | Next.js 15 (App Router) + React + shadcn/ui + Tailwind CSS |
| IaC | AWS CDK (Python) |
| Auth | Amazon Cognito |
| Data Store | Aurora PostgreSQL (Cache), DynamoDB, S3, S3 Tables (Iceberg archive) |
| Knowledge Base | Bedrock Knowledge Bases + S3 Vectors |

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

| Path | Protocol | 용도 | 중간 레이어 |
|---|---|---|---|
| **A: SSE Direct** | SSE over HTTP | AI 대화, 쿼리 분석, 장애 진단 | 없음 (AgentCore Runtime 직접) |
| **B: REST API** | HTTP REST | 대시보드, 메트릭 조회, 클러스터 목록 | API Gateway + Lambda |
| **C: REST + Async** | HTTP REST → Async | 승인 워크플로, DDL/DML 실행 | API Gateway + Lambda → AgentCore |

Path A에서 브라우저가 AgentCore Runtime에 직접 SSE 연결한다. Cognito JWT로 인증하며 중간에 Lambda/API Gateway를 두지 않는다. 이는 ChatGPT, Claude.ai 등 업계 표준 패턴과 동일하다.

### 2.3 Design Principles

1. **CDK-First**: 모든 인프라 변경은 CDK를 통해서만 수행. AWS CLI 직접 수정 금지.
2. **Single Agent + Gateway**: Multi-Runtime 호출의 지연/토큰 문제를 피하기 위해 단일 AgentCore Runtime + Gateway MCP 구조 채택.
3. **Shared Data Store**: AI 에이전트와 대시보드가 동일 데이터 저장소(Aurora PG Cache)를 조회. AI 전용 데이터 경로 없음.
4. **Gateway Semantic Search**: 42개 도구 중 질문당 5-10개만 동적 로드하여 tool explosion 방지.
5. **Human-in-the-loop**: 조회는 자동, 변경은 DBA 승인 필수. Cedar Policy로 강제.
6. **Self-Service Deployment**: `cdk deploy`만으로 전체 스택 배포 가능. 환경별 설정은 config 파일로 분리.

---

## 3. Data Layer

### 3.1 Data Store Architecture

| Store | 용도 | 데이터 | 보존 |
|---|---|---|---|
| **Aurora PG (Cache)** | Hot tier, 실시간 대시보드 + AI 조회 | 메트릭 스냅샷, 쿼리 통계, 클러스터 메타, 인덱스 사용률, 슬로우 쿼리 | 7일 (1분 해상도) |
| **DynamoDB** | 세션, 승인, 이벤트 | 대화 세션, 승인 이력, RDS 이벤트, 알림 이력 | 90일 |
| **S3** | 장기 보관, 아카이브 | EXPLAIN Plan, 리포트, 집계 데이터 | 1년+ |
| **S3 Tables (Iceberg)** | Cold tier, 장기 분석 | 7일 지난 메트릭 (5분/1시간 집계) | 1년 |
| **Bedrock KB + S3 Vectors** | RAG 지식 베이스 | Aurora 문서, 런북, best practice | 상시 |

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

| Collector | 주기 | 수집 대상 | 저장소 |
|---|---|---|---|
| **PI Collector** | 1분 | AAS, wait events, counter metrics | Aurora PG (metric_snapshots) |
| **Stats Collector** | 5분 | pg_stat_statements, connection stats, replication lag | Aurora PG (query_stats, connection_stats) |
| **Meta Collector** | 5분 | 클러스터 메타데이터, 인스턴스 상태 | Aurora PG (cluster_meta) |
| **Structure Collector** | 1시간 | 인덱스 사용률, 테이블 bloat, 스키마 스냅샷, 파라미터 | Aurora PG (index_usage, schema_snapshots) |
| **Event Processor** | 실시간 | RDS Events, CloudWatch Alarms, Aurora 이벤트 | DynamoDB (event_history) + SNS |
| **Archive Job** | 1일 | 7일 지난 고해상도 데이터 → 집계 | S3 Tables (Iceberg) |

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

Gateway는 5개 MCP Server를 단일 MCP 엔드포인트로 통합한다.

기능:
- **Semantic Tool Search**: `x_amz_bedrock_agentcore_search`로 자연어 기반 도구 검색
- **Cedar Policy Engine**: 도구별/사용자별/입력값별 세밀한 권한 제어
- **MCP Sessions**: 세션 기반 연결로 후속 호출 레이턴시 감소

### 4.3 3-Tier Knowledge Strategy

```
Tier 1: System Prompt 치트시트 (항상 포함, ~2,000 토큰)
  - Aurora 핵심 파라미터 30개 요약
  - 공통 진단 워크플로
  - 위험 작업 판단 기준

Tier 2: Bedrock KB + S3 Vectors (retrieve 도구, ~100ms)
  - Aurora 공식 문서
  - 내부 런북, best practice, 장애 보고서

Tier 3: AWS Docs MCP (on-demand, 1-5초)
  - 최신 릴리즈 노트, 신규 기능
  - KB 결과 불충분 시 fallback
```

에이전트 판단 기준: 기본 → Tier 2(retrieve), "최신"/"업데이트" 키워드 또는 KB 결과 부족 → Tier 3(AWS Docs MCP).

---

## 5. MCP Servers

### 5.1 Overview

| MCP Server | 도구 수 | Cedar Policy | Gateway Target |
|---|---|---|---|
| Performance | 15 | READ-ONLY | Lambda |
| Incident | 8 | READ-ONLY | Lambda |
| Operations | 11 | MIXED (read auto / write approval) | Lambda |
| Simulation | 6 | READ-ONLY | Lambda |
| Knowledge | 2 | READ-ONLY | Strands native + MCP |
| **Total** | **42** | | |

### 5.2 Performance MCP Server (15 tools)

**데이터 조회 (6):**
- `get_top_queries` — Aurora PG Cache에서 Top-N 쿼리 (총 시간/호출 수/평균 시간 기준)
- `explain_query` — Target Aurora에서 EXPLAIN ANALYZE 실행 + S3에 실행 계획 저장
- `get_pi_metrics` — Aurora PG Cache에서 PI 메트릭 (AAS, wait events, counter metrics)
- `recommend_index` — index_usage + query_stats 조합 분석으로 인덱스 추천
- `get_slow_queries` — Aurora PG Cache에서 슬로우 쿼리 목록 (MySQL: slow query log 파싱, PG: pg_stat_statements 기반)
- `compare_periods` — 두 기간의 메트릭 비교 분석

**분석 (4):**
- `detect_anomalies` — 최근 N시간 메트릭을 7일 이동평균 baseline 대비 z-score로 이상 탐지
- `detect_regressions` — 특정 시점 전후 쿼리 성능 비교 (배포 후 느려진 쿼리 탐지)
- `forecast_capacity` — 스토리지/연결 수 선형 회귀 예측 (N일 후 한계 도달 예상)
- `get_performance_summary` — 지정 기간 핵심 KPI 요약 (avg_aas, top_waits, slow_query_count 등)

**상세 모니터링 (5):**
- `get_lock_analysis` — blocking 세션 트리 구성 (innodb_lock_waits / pg_locks)
- `get_vacuum_stats` — (PG) autovacuum 현황, dead tuples, bloat ratio
- `get_replication_status` — Reader 지연, 복제 슬롯, Global DB 지연, failover 우선순위
- `get_connection_analysis` — 연결 풀 상태, idle 연결, 앱별 분류, max_connections 대비 사용률
- `get_cost_analysis` — 인스턴스 비용 추이, RI/SP 추천, I/O 비용 분석

### 5.3 Incident MCP Server (8 tools)

- `get_health_status` — 클러스터 건강 상태 종합 (인스턴스 상태, 연결, 복제 지연)
- `get_recent_events` — RDS Events, 알람, failover 이력
- `search_logs` — CloudWatch Logs Insights로 Aurora error/audit log 검색
- `get_alarm_history` — CloudWatch 알람 상태 변경 이력
- `correlate_signals` — 메트릭 + 이벤트 + 로그를 시간축 정렬하여 장애 타임라인 구성
- `get_connections` — 현재 활성 세션 목록 (SHOW PROCESSLIST / pg_stat_activity)
- `get_incident_summary` — 최근 N일 장애/이벤트 통계 (MTTR, 빈도, 유형별 분류)
- `find_similar_incidents` — Bedrock KB에서 현재 증상과 유사한 과거 장애 사례 검색

### 5.4 Operations MCP Server (11 tools)

**조회 (5, 자동 허용):**
- `get_parameters` — Target Aurora의 현재 파라미터 값
- `get_backup_status` — 백업 이력 및 상태
- `get_scaling_info` — 현재 용량, ACU, 스토리지
- `get_schema_diff` — 두 환경/시점 간 스키마 비교
- `get_schema_history` — 스키마 변경 이력 추적

**실행 (4, 승인 필요):**
- `execute_sql` — SQL 실행 (SELECT 자동 허용, DDL/DML 승인 필요)
- `modify_parameter` — DB 파라미터 변경
- `modify_scaling` — 인스턴스 스케일링
- `manage_maintenance` — 유지보수 윈도우 관리

**분석 (2, 자동 허용):**
- `review_sql` — DDL/DML 실행 전 자동 리뷰 (위험도, 영향 행 수, 락 시간 추정, 롤백 SQL)
- `audit_permissions` — DB 사용자/역할 권한 감사 (과도한 권한, 미사용 계정 탐지)

### 5.5 Simulation MCP Server (6 tools)

- `check_upgrade_compatibility` — 버전 업그레이드 호환성 체크 (deprecated 기능, 새 기능, 호환 SQL)
- `estimate_upgrade_impact` — 업그레이드 방식별 예상 시간/다운타임/리스크 분석
- `generate_upgrade_plan` — 업그레이드 실행 계획서 생성 (체크리스트, 절차, 롤백 계획)
- `simulate_parameter_change` — 파라미터 변경 영향 분석 (static/dynamic, 영향 범위, 연관 파라미터)
- `simulate_scaling` — 스케일 업/다운 비용-성능 트레이드오프 분석
- `simulate_ddl_impact` — DDL 영향도 분석 (테이블 크기, 예상 락 시간, 온라인 DDL 가능 여부)

### 5.6 Knowledge (Strands Native, 2 tools)

- `retrieve` — Bedrock KB + S3 Vectors 검색 (Tier 2). Strands 네이티브 도구로 Agent에 직접 등록.
- `aws_docs` — AWS Documentation MCP Server 조회 (Tier 3). Gateway에 외부 MCP Server 타겟으로 등록하여 연결.

---

## 6. Safety & Policy

### 6.1 5-Layer Safety Model

| Layer | 구현 | 역할 |
|---|---|---|
| **L1: Query Sandbox** | MCP Server 내 read-only DB 연결 기본 | 의도치 않은 쓰기 방지 |
| **L2: SQL Audit Trail** | 모든 에이전트 쿼리에 `/* source=dbops-agent */` 주석 | 추적성 |
| **L3: Cedar Policy** | AgentCore Policy Engine | 도구/사용자/입력값 레벨 제어 |
| **L4: Human-in-the-loop** | 승인 워크플로 (DynamoDB + Web UI) | 변경 작업 DBA 승인 |
| **L5: Dry-run Mode** | EXPLAIN만 실행, 결과 미리보기 | 실행 전 영향 확인 |

### 6.2 Cedar Policy Examples

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

| Page | 데이터 소스 | 통신 경로 |
|---|---|---|
| **Chat** | AgentCore Runtime | Path A (SSE Direct) |
| **Dashboard** | Aurora PG Cache | Path B (REST API) |
| **Clusters** | Aurora PG Cache + DynamoDB | Path B (REST API) |
| **Query Lab** | AgentCore Runtime | Path A (SSE Direct) |
| **Approval Center** | DynamoDB | Path B + C (REST + Async) |
| **Reports** | S3 + DynamoDB | Path B (REST API) |

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

| Phase | 방식 | 용도 |
|---|---|---|
| Phase 1-3 | **RDS Data API** | 네트워크 설정 없이 HTTPS로 SQL 실행 |
| Phase 4+ | **Transit Gateway** (선택) | 직접 TCP 연결 필요 시 (고성능, 다수 계정) |

### 8.3 Cluster Registration

DBA가 Web UI에서 클러스터를 등록하면:
1. Account ID, Region, Cluster ID, Spoke Role ARN 입력
2. 연결 테스트 (Hub → AssumeRole → Spoke → rds:DescribeDBClusters)
3. DynamoDB cluster_registry 테이블에 저장
4. Data Collection Pipeline이 자동으로 해당 클러스터 수집 시작

---

## 9. CDK Infrastructure

### 9.1 Stack Structure

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

| Phase | Perf | Incident | Ops | Sim | Knowledge | Total |
|---|---|---|---|---|---|---|
| 1 | 6 | - | - | - | 2 | 8 |
| 2 | 10 | 8 | - | - | 2 | 20 |
| 3 | 10 | 8 | 11 | - | 2 | 31 |
| 4 | 10 | 8 | 11 | 6 | 2 | 37 |
| 5 | 15 | 8 | 11 | 6 | 2 | 42 |

---

## 12. Research References

### Architecture Decisions

| Decision | Chosen | Rejected | Reason |
|---|---|---|---|
| Agent topology | Single Agent + Gateway | Multi-Agent Runtime | Multi-Runtime 호출 시 지연/토큰 문제 (이전 AIOps 실전 경험) |
| Tool routing | Gateway Semantic Search | Custom Haiku Classifier | Gateway 내장 기능으로 별도 구현 불필요 |
| Streaming | SSE Direct to Runtime | Lambda proxy | Lambda proxy 시 SSE 스트리밍 불가 |
| Metrics cache | Aurora PG | S3 Tables + Athena | Athena 레이턴시 2-10초, 대시보드 <500ms 불가. 비용도 10-50x 높음 |
| Vector store | S3 Vectors | OpenSearch Serverless | OpenSearch 최소 $700/월 vs S3 Vectors $5-10/월 |
| RAG strategy | 3-Tier Hybrid | Bedrock KB only | 최신성(AWS MCP) + 빠른 응답(치트시트) + 내부 문서(KB) 모두 필요 |
| Cross-account network | RDS Data API (초기) | VPC Peering | 네트워크 설정 없이 즉시 시작 가능 |
| UI design | Claude Design → Claude Code | Code-only | AI 생성 느낌 방지, 제품다운 디자인 품질 확보 |

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
