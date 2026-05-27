# DBOps — AI-Powered Database Operations Platform

AI 기반 종합 데이터베이스 운영 플랫폼. 자연어 대화로 Amazon Aurora MySQL/PostgreSQL 클러스터의 성능 분석, 장애 진단, 운영 자동화, 시뮬레이션을 수행합니다.

## Features

- **AI Chat** — 자연어로 DB 성능 분석, 장애 진단, 운영 작업 요청
- **Performance Analysis** — Slow query 분석, EXPLAIN plan tree + anti-pattern auto-detection, 인덱스 추천, 이상 탐지
- **Incident Diagnosis** — RCA(Root Cause Analysis), 시그널 상관 분석, 타임라인 재구성
- **Operations Automation** — 파라미터 변경, DDL 실행, 스케일링 (Human-in-the-loop 승인)
- **Simulation UI** — 버전 업그레이드 (compatibility + method matrix + ordered plan), parameter 변경, ACU 스케일링 비용, DDL 영향을 채팅 없이 `/simulator` 페이지에서 즉시 추정
- **Monitoring Dashboard** — 실시간 클러스터 상태, 메트릭 시각화, 프로액티브 알림
- **Cross-Account** — Hub-Spoke IAM 패턴으로 여러 AWS 계정의 Aurora 클러스터 통합 관리
- **SLO Tracker** — 가용성 + p-mean 쿼리 지연 SLO 목표 대비 실측 + 에러 버짓 burn-down (`/slo`)
- **Schema Lineage** — `pg_constraint` 라이브 introspection으로 외래키 관계 그래프 시각화 (`/schema`)
- **Replication Topology** — Writer + Readers + 인스턴스별 AuroraReplicaLag, promotion tier, multi-AZ (Dashboard 패널)
- **Redundant Indexes** — Prefix-covered / 완전 중복 / unused 인덱스 자동 검출 (Dashboard 패널, PG)
- **Capacity Forecasting** — Storage / Connections / AAS 30-60-90 day 선형 회귀 예측 + 임계 도달 시점
- **PG Log Insights** — CloudWatch Logs Insights를 카테고리(slow/vacuum/error/connection)별로 묶어 Dashboard에서 조회
- **Cost Anomaly Detection** — Bedrock 일별 사용액 spike를 z-score + 절대 차이 + 상대 비율 triple gate로 감지 (`/cost`)
- **Compound Alert Rules** — 단일 threshold + AND/OR DSL 양쪽 지원 (per-operand window/agg). Slack 양방향 ack 가능 — 알림 메시지의 "✓ Ack" 버튼을 누르면 알림 페이지에 즉시 반영 (Alerts → "Slack 양방향 Ack 설정" 가이드)

## Architecture

```
Web UI (Next.js) ──SSE──▶ AgentCore Runtime (Strands Agent)
                                    │
                              AgentCore Gateway (Cedar Policy)
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              Custom MCP      Official AWS MCP   Bedrock KB
              (Performance,   (Aurora PG/MySQL,  (S3 Vectors)
               Incident,      CloudWatch,
               Operations,    AWS API)
               Simulation)
                    │
                    ▼
             Aurora PG Cache ◀── Data Collection Pipeline
```

- **Single Agent + Gateway**: 단일 AgentCore Runtime + Gateway MCP로 지연/토큰 최적화
- **CDK-First**: 모든 인프라는 CDK로만 관리. `cdk deploy --all`로 전체 배포
- **Human-in-the-loop**: 조회는 자동, 변경은 DBA 승인 필수 (Cedar Policy 강제)

## Tech Stack

| Layer    | Technology                                          |
| -------- | --------------------------------------------------- |
| Agent    | Strands Agents SDK, AgentCore Runtime/Gateway       |
| LLM      | Amazon Bedrock Claude                               |
| Frontend | Next.js 15, React, shadcn/ui, Tailwind CSS          |
| IaC      | AWS CDK (Python)                                    |
| Data     | Aurora PostgreSQL (Cache), DynamoDB, S3, S3 Vectors |

## Quick Start

### Prerequisites

- AWS Account with AdministratorAccess
- Node.js 20+, Python 3.10+
- AWS CDK CLI (`npm install -g aws-cdk`)
- Bedrock model access enabled (Claude Sonnet)

### Deployment (Quickstart)

```bash
# 1. Clone + configure (two values: ACCOUNT_ID + REGION)
git clone https://github.com/JinHyun-Park/dbops.git
cd dbops
cp cdk/config/settings.example.py cdk/config/settings.py
$EDITOR cdk/config/settings.py     # set ACCOUNT_ID, REGION (rest is optional)

# 2. Bootstrap CDK once per account/region
cd cdk && cdk bootstrap && cd ..

# 3. Run the all-in-one deployer
./deploy.sh
# Builds frontend → bundles SQL schemas → builds ARM64 agent deps →
# deploys all CDK stacks (foundation, data, agent, frontend) →
# SchemaMigrator Custom Resource creates 14+ tables idempotently →
# Cognito Callback URLs auto-registered to your CloudFront domain →
# /config.json deployed with your apiUrl, region, cognitoClientId, agentRuntimeArn.
```

The final output shows your Web UI URL. Open it, log in (Cognito Hosted UI),
and register your Aurora cluster from the Clusters page (cross-account spoke
roles are validated via STS AssumeRole at registration time).

#### One-time post-deploy: activate Bedrock cost-allocation tags

CDK creates six Application Inference Profiles tagged with `Application=DBOps`,
`Environment={env}`, `CostCategory=DBOps`. To populate the `/cost` page with
real spend you must activate the tag in your billing preferences once:

1. Open the [Cost allocation tags console](https://console.aws.amazon.com/billing/home#/preferences/tags)
2. Find **Application** under "User-defined cost allocation tags"
3. Select it → click **Activate**
4. Wait ~24h. Cost Explorer is not retroactive, but new chat invocations are
   immediately attributed to the profile from then on.

#### Optional post-deploy: Slack 양방향 Ack

Outbound Slack 알림 메시지의 "✓ Ack" 버튼이 동작하려면 Slack 앱 측 설정이 한 번 필요합니다. **Alerts 페이지의 "Slack 양방향 Ack 설정" 섹션에서 4단계 가이드 + endpoint URL을 자동으로 받을 수 있습니다.** (PageHeader 아래의 "셋업 가이드 열기" 버튼).

요약:

1. [api.slack.com/apps](https://api.slack.com/apps?new_app=1)에서 새 Slack 앱 생성
2. **Basic Information → Signing Secret** 복사 → `cdk/config/settings.py`의 `SLACK_SIGNING_SECRET`에 붙여넣기
3. **Interactivity & Shortcuts** 활성화 → Request URL에 `{API_GATEWAY_URL}/api/slack/interactive` 입력
4. **Incoming Webhooks** 활성화 → 채널 webhook URL을 Alerts 페이지 Subscribers에 `slack-webhook` 프로토콜로 등록 → `cdk deploy dbops-dev-agent` 한번 더

Signing Secret이 비어 있어도 outbound Slack 메시지(읽기) + 모든 다른 기능은 정상 작동합니다. 양방향 ack만 비활성화됩니다.

#### Optional post-deploy hardening

```bash
# Tighten CORS — by default Lambdas echo request Origin (dev-safe).
# Set ALLOWED_ORIGINS on dashboard/alerts Lambdas to your CloudFront domain
# in production. (Auto-injection would create a cyclic CFN dependency.)

# Configure AgentCore (post-deploy)
# npm install -g @aws/agentcore
# agentcore create --defaults
# agentcore deploy

# 8. Register your first cluster
# Open the Web UI → Clusters → + Register
```

> **Cognito Callback URLs are auto-registered**: the frontend stack uses an
> AWS Custom Resource to add `<DistributionUrl>/callback` (and localhost)
> to the User Pool client at deploy time. No manual Cognito setup needed.
>
> **Runtime config**: the frontend fetches `/config.json` at boot, so the
> built static bundle is portable. CDK writes `apiUrl`, `frontendUrl`,
> `region`, `cognitoDomain`, `cognitoClientId`, and `agentRuntimeArn` into
> the config object, resolved from the live stack outputs at deploy time.

### Cluster Credential Setup (production-recommended)

DBOps reads cluster monitoring data over the RDS Data API. By default the
master secret discovered alongside each Aurora cluster is used, which works
out-of-the-box but grants DBOps the same blast radius as the admin user.

For production, create a dedicated `dbops_readonly` role on each cluster and
register its credentials in Secrets Manager using the **DBOps naming
convention** — bulk Discover finds and attaches it automatically, no manual
ARN entry needed:

```
secret name: dbops/<cluster_id>/readonly
secret JSON: {"username":"dbops_readonly","password":"..."}
```

The Clusters page has a **📋 Setup guide** button that walks through the
SQL + AWS CLI snippets for both PostgreSQL and MySQL. In short:

**PostgreSQL**

```sql
CREATE ROLE dbops_readonly LOGIN PASSWORD '...';
GRANT pg_monitor, pg_read_all_settings, pg_read_all_stats TO dbops_readonly;
```

**MySQL**

```sql
CREATE USER 'dbops_readonly'@'%' IDENTIFIED BY '...';
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'dbops_readonly'@'%';
GRANT SELECT ON performance_schema.* TO 'dbops_readonly'@'%';
GRANT SELECT ON information_schema.* TO 'dbops_readonly'@'%';
GRANT SELECT ON mysql.* TO 'dbops_readonly'@'%';
```

Then register the credentials:

```bash
aws secretsmanager create-secret \
  --region <region> \
  --name "dbops/<cluster_id>/readonly" \
  --secret-string '{"username":"dbops_readonly","password":"<password>"}'
```

After this, the Discover table shows one of three badges per cluster:

- `✓ convention` — dedicated user found and auto-attached (recommended)
- `⚠ master fallback` — using master secret; works but should be tightened
- `✗ missing` — no usable secret; needs setup before activation

### Cross-Account Setup

To manage Aurora clusters in other AWS accounts:

1. Deploy the spoke role in each target account:

   ```bash
   aws cloudformation deploy \
     --template-file cdk/cross-account/spoke-role-template.yaml \
     --stack-name dbops-spoke-role \
     --parameter-overrides HubAccountId=<HUB_ACCOUNT_ID> \
     --capabilities CAPABILITY_NAMED_IAM
   ```

2. Tag clusters for write access: `ManagedBy=dbops`

3. Register in DBOps with the spoke role ARN

See `cdk/cross-account/README.md` for details.

## Project Structure

```
dbops/
├── cdk/                  # CDK infrastructure (4 stacks)
├── agent/                # Strands Agent + Dockerfile
├── mcp-servers/          # 4 Custom MCP servers (30 tools)
├── data-pipeline/        # ETL, Event Processor, Report Generator, Monitor
├── api/                  # REST API Lambdas
├── frontend/             # Next.js Web UI
├── knowledge/            # Bedrock KB source documents
├── tests/                # Unit tests
├── .kiro/                # Kiro specs and steering
└── docs/                 # Design specs and plans
```

## Development

```bash
# Run tests
pytest tests/ -v

# Frontend dev server
cd frontend && npm run dev

# CDK synth (validate templates)
cd cdk && cdk synth --quiet
```

### One-time automation setup

Install once per clone — afterwards every commit / push is auto-checked:

```bash
# Dev deps (pytest, ruff, pre-commit, CDK synth deps)
pip install -r requirements-dev.txt

# Install pre-commit hooks (ruff + prettier + tsc --noEmit on TS changes)
pre-commit install
```

GitHub Actions (`.github/workflows/ci.yml`) re-runs the same checks on
every push/PR — three parallel jobs:

- **python** — `ruff check .` + `pytest tests/unit`
- **cdk** — `cdk synth` smoke + 4-stack snapshot test
- **frontend** — `tsc --noEmit` + `next build`

Claude Code hooks (`.claude/hooks/`) add two structural gates while
working with the assistant:

- **`pre-commit-review.sh`** — blocks `git commit` until the code-reviewer
  subagent runs. Bypass for trivial diffs by adding `[skip-review]` to the
  commit message.
- **`stop-session-memory.sh`** — at session end, surfaces commits since
  the last checkpoint and reminds the assistant to persist a project
  memory note so the next session resumes cleanly.

## Troubleshooting

### Fleet page에 등록한 적 없는 클러스터가 보임

PG 캐시(`cluster_meta`)에 과거 ETL이 남긴 행이 있고 DDB 레지스트리에는 없는 경우. 현재 build는 `_multi_cluster_overview`가 DDB와 자동 교차 검증하므로 신규 deploy 이후엔 안 보입니다. 기존 캐시 청소가 필요하면 `cluster_meta` + per-cluster 테이블에서 해당 `cluster_id` DELETE.

### Cost 페이지에 $0만 표시

`Application` cost allocation 태그가 billing console에서 활성화되지 않은 경우. Quick Start의 "post-deploy: activate Bedrock cost-allocation tags" 단계 확인. 활성화 후에도 과거 비용은 backfill되지 않고 그 시점 이후 호출분만 집계됩니다.

### Slack "✓ Ack" 버튼 클릭 시 "SLACK_SIGNING_SECRET not configured" 메시지

`cdk/config/settings.py`의 `SLACK_SIGNING_SECRET`이 비어 있음. 위 "Slack 양방향 Ack" 가이드의 4단계 수행 + agent 스택 재배포 필요.

### Schema lineage / Replication topology 패널이 "MySQL은 v1에서 지원하지 않습니다"

PostgreSQL 전용 기능들입니다. MySQL 클러스터에서는 friendly 안내가 표시되며 다른 패널은 정상 동작합니다.

### Bedrock 응답이 비정상적으로 느림

Cold start 또는 region capacity 이슈. CloudWatch에서 AgentCore Runtime 로그 확인. AGENT_MODEL_ID를 가벼운 모델로 임시 전환해서 비교: `settings.py`의 `AGENT_MODEL_ID`를 Haiku로.

### 라이트 모드에서 일부 텍스트가 안 보임

Recharts 범례 색상은 inline SVG attr로 주입되어 CSS override가 닿지 않는 경우가 있음. 패턴이 보이면 `frontend/src/lib/use-chart-colors.ts`에 매핑 추가 후 해당 컴포넌트에서 hook을 통해 색상을 받도록 수정. WCAG AA(4.5:1) 목표.

## Documentation

- Design Spec: `docs/superpowers/specs/2026-05-08-dbops-design.md`
- Implementation Plans: `docs/superpowers/plans/`
- Kiro Specs: `.kiro/specs/`
- Cedar Policies: `cdk/policies/`
- Cross-Account: `cdk/cross-account/README.md`

## License

Private — All rights reserved
