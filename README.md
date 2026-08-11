# DBOps — AI-Powered Database Operations Platform

AI 기반 종합 데이터베이스 운영 플랫폼. 자연어 대화로 Amazon Aurora(PostgreSQL·MySQL), RDS for MySQL·SQL Server(비-Aurora 독립형 인스턴스), DocumentDB, DynamoDB, ElastiCache(Redis/Valkey/Memcached)의 성능 분석, 장애 진단, 운영 자동화, 시뮬레이션을 수행합니다.

## Features

DBA를 위한 풀스택 운영 플랫폼 — **대화로 진단하고, 안전하게 실행하고, fleet 전체를 모니터링**합니다.
아래는 영역별 핵심 기능이며, 각 항목의 `/path`는 해당 Web UI 페이지입니다.

<details open>
<summary><b>🤖 AI & 대화</b></summary>

- **AI Chat** (`/chat`) — 자연어로 성능 분석·장애 진단·운영 작업 요청. AWS MCP Server(SigV4)로 **공식 AWS/Aurora 문서를 근거로 인용**해 답변
- **Ask the Fleet** (`/ask`) — "CPU 80% 넘은 클러스터 보여줘" 같은 자연어 fleet 조회. NL→filter compiler + saved views
- **AI Runbooks** (`/runbooks`) — 채팅 진단/처방을 마크다운 playbook으로 저장·검색·재사용
- **Cross-Device Chat Sessions** — 대화가 DynamoDB에 영속화돼 다른 기기/브라우저에서 이어쓰기 (1.5s debounced sync + offline 캐시 + 90-day TTL)
- **Agent Memory Inspector** (`/preferences`) — AgentCore Memory의 preferences/facts 조회·삭제. Cognito sub 기반 namespace로 cross-user read 차단

</details>

<details>
<summary><b>📊 성능 & 분석</b></summary>

- **Performance Analysis** — Slow query 분석, EXPLAIN plan tree + anti-pattern 자동 검출, 인덱스 추천, 이상 탐지
- **Schema Lineage** (`/schema`) — `pg_constraint` 라이브 introspection으로 FK 관계 그래프 시각화
- **Replication Topology** (Dashboard) — Writer/Readers + 인스턴스별 AuroraReplicaLag, promotion tier, multi-AZ
- **Redundant Indexes** (Dashboard) — prefix-covered / 완전 중복 / unused 인덱스 자동 검출
- **Capacity Forecasting** — Storage/Connections/AAS 30·60·90일 선형 회귀 예측 + 임계 도달 시점
- **PG Log Insights** + **Keyword Search** (`/dashboard`) — CloudWatch Logs Insights를 카테고리별로 묶어 조회, 검색어 AND 조인 + regex 살균
- **Saved Query Library** (Query Lab) — 자주 쓰는 SQL 저장·태깅·cross-device 로드
- **MySQL Dashboard Parity** — Schema/Indexes/Log Insights 모두 Aurora MySQL 지원

</details>

<details>
<summary><b>📈 모니터링 & 알림</b></summary>

- **Monitoring Dashboard** (`/dashboard`) — 실시간 클러스터 상태, 메트릭 시각화, Health Score
- **Fleet Overview** (`/fleet`) — 전체 클러스터 한눈에. ETL 신선도 배지(fresh/stale/no_data) 포함
- **SLO Tracker** (`/slo`) — 가용성 + p-mean 쿼리 지연 SLO 실측 + 에러 버짓 burn-down
- **Compound Alert Rules** (`/alerts`) — 단일 threshold + AND/OR DSL(per-operand window/agg). DBA 프리셋 6종 + Slack 양방향 Ack
- **Alert Impact** — 알람 ±5min의 슬로우 쿼리·동시 이벤트·동시 알람을 인라인 패널로 (사고 triage)
- **Cost Anomaly Detection** (`/cost`) — Bedrock 일별 사용액 spike를 z-score + 절대차 + 상대비 triple gate로 감지

</details>

<details>
<summary><b>🔧 운영 & 안전장치</b></summary>

- **Operations Automation** — 파라미터 변경, DDL 실행, 스케일링, **스냅샷·복원** (전부 Human-in-the-loop 승인)
- **Approval Guard** — 모든 write tool이 서버측에서 DDB approval row를 검증. agent가 `approved=true`를 임의로 못 켜고, `approval_id`(DBA가 `/approvals`에서 승인 시 발급) + cluster/action_type/30분 윈도우/atomic consume까지 강제
- **Simulation UI** (`/simulator`) — 업그레이드(호환성+method matrix+ordered plan)·파라미터·ACU 비용·DDL 영향을 채팅 없이 즉시 추정

</details>

<details>
<summary><b>🚨 인시던트 & 감사</b></summary>

- **Incident Diagnosis** — RCA, 시그널 상관 분석, 타임라인 재구성
- **Incident Timeline** (`/timeline`) — 한 cluster의 모든 신호(알람/RDS 이벤트/스키마 변경/proactive/Slack ack/실행된 쓰기)를 시간축 한 줄에, 카테고리 칩 필터
- **DBOps Activity Log** (`/activity`) — 누가 무엇을 요청/승인/실행했는지 시간순 기록 (컴플라이언스 감사 + 사후 회고). `query_activity_audit` MCP 도구로 채팅에서도 질의 가능
- **Daily Operations Report** (`/reports`) — `report_generator` Lambda가 매일 자정 24h 메트릭을 집계 + Bedrock Claude로 한국어 요약 (실패 시 템플릿 fallback)

</details>

<details>
<summary><b>🏢 플랫폼 & 멀티계정</b></summary>

- **Cross-Account** — Hub-Spoke IAM 패턴으로 여러 AWS 계정의 Aurora 통합 관리
- **Cluster Registration Wizard** (`/clusters`) — same/cross-account 모드 토글, "연결만 테스트" 3-step pre-flight (STS AssumeRole + DescribeDBClusters + master secret)
- **Schema Migration Auto-Trigger** — `cdk deploy` 시 SQL 디렉터리 SHA-256 해시를 schema_version에 주입해 변경 시 자동 마이그레이션

</details>

## Architecture

```
Web UI (Next.js, static) ──SSE──▶ AgentCore Runtime (Strands Agent)
                                    │                        │
                          AgentCore Gateway          AWS MCP Server
                          (Cedar Policy)             (SigV4 · 공식 AWS 문서)
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼            ▼              ▼               ▼
         Performance   Incident      Operations       Simulation
           MCP          MCP            MCP               MCP
                       (4 custom MCP servers · 64 tools)
                                    │
                                    ▼
                  Aurora PG Cache ◀── Data Collection Pipeline
                  (hot cache)         (ETL · Event Processor · Report · Monitor)
```

- **Custom 도구는 Gateway 경유** (write 승인은 tool-level `approval_guard`가 강제 — Cedar Policy Engine은 게이트웨이에 LOG_ONLY로 바인딩된 방어심층), **공식 AWS 문서는 AWS MCP Server에 SigV4로 직접** — 읽기 전용 문서 도구만 노출
- **Dashboard 데이터는 사전 수집 캐시에서** — 실시간 렌더링 중 AWS API를 직접 호출하지 않음 (라이브 패널만 예외: topology/backup)

- **Single Agent + Gateway**: 단일 AgentCore Runtime + Gateway MCP로 지연/토큰 최적화
- **CDK-First**: 모든 인프라는 CDK로만 관리. `cdk deploy --all`로 전체 배포
- **Human-in-the-loop**: 조회는 자동, 변경은 DBA 승인 필수 (tool-level `approval_guard` 강제 — fail-closed·payload-hash 바인딩·single-use)

## Tech Stack

| Layer    | Technology                                            |
| -------- | ----------------------------------------------------- |
| Agent    | Strands Agents SDK, AgentCore Runtime/Gateway         |
| LLM      | Amazon Bedrock Claude                                 |
| Frontend | Next.js 16, React, shadcn/ui, Tailwind CSS            |
| IaC      | AWS CDK (Python)                                      |
| Data     | Aurora PostgreSQL (hot cache), DynamoDB, S3 (archive) |

## Quick Start

### Prerequisites

- AWS Account with AdministratorAccess
- Node.js 20+, Python 3.10+
- AWS CDK CLI (`npm install -g aws-cdk`)
- **Bedrock model access** enabled for BOTH of:
  - a Claude text model, via the cross-region inference profile for your region
    (`apac.` / `us.` / `eu.` / `global.` prefix). This is `AGENT_MODEL_ID`.
  - **Amazon Titan Text Embeddings V2** (`amazon.titan-embed-text-v2:0`). Semantic
    incident search embeds into pgvector with it (`incident_embeddings` collector and
    the `find_similar_incidents` tool), so without access those fall back to keyword
    matching while everything else looks healthy.
- A region where **Bedrock AgentCore** (Runtime, Gateway, Memory) is available. The
  agent stack cannot deploy without it, and it is not in every region.

### Running cost

This is not free tier. Two components bill whether or not anyone logs in:

| always-on                               | measured                     |
| --------------------------------------- | ---------------------------- |
| Aurora Serverless v2 cache, min 0.5 ACU | about $70/month              |
| one NAT Gateway (private Lambda egress) | about $37/month (hours only) |

So roughly **$110/month before any traffic**, plus usage: Lambda (scales with
registered-cluster count times the 5-minute ETL), Bedrock tokens, CloudFront, DynamoDB
and S3.

For scale: this project's own dev account, measured over the 30 days to 2026-08-11 with
`Application=DBOps` cost allocation, came to $485. Do not read that as your bill: most of
it is the demo TARGET databases being monitored, and one provisioned `db.r6g.large`
Aurora among them is about $180/month on its own. The platform's own share was closer to
$200/month with 11 registered clusters.

### Deployment (Quickstart)

```bash
# 1. Clone + configure
git clone https://github.com/JinHyun-Park/dbops.git
cd dbops
cp cdk/config/settings.example.py cdk/config/settings.py
$EDITOR cdk/config/settings.py
#   REQUIRED: ACCOUNT_ID, REGION
#   CHECK:    AGENT_MODEL_ID must carry the inference-profile prefix for YOUR
#             region (apac. / us. / eu. / global.). Claude Sonnet 4 has no bare
#             on-demand model ID, so a wrong prefix fails every chat turn.
#   OPTIONAL: COGNITO_DOMAIN_PREFIX. Leave it EMPTY and the stack derives a
#             prefix unique to your account (the Hosted UI prefix is globally
#             unique per region, so a shared literal collides).

# 2. Bootstrap CDK once per account/region, FROM OUTSIDE THE REPO.
#    `cdk bootstrap` synthesizes the app whenever it is run in a directory that has a
#    cdk.json, and synth needs build artifacts that step 3 is what produces. Running it
#    from a directory with no CDK app skips synthesis entirely. Verified 2026-08-12 in a
#    clean account: from the repo it fails on the missing artifacts, from an empty
#    directory it bootstraps in about 40 seconds.
#    Passing the target explicitly is NOT enough on its own: `aws://ACCOUNT/REGION` still
#    synthesizes when a cdk.json is present.
(cd /tmp && cdk bootstrap aws://<ACCOUNT_ID>/<REGION>)

# 3. Run the all-in-one deployer
./deploy.sh
# Builds frontend → bundles SQL schemas → builds ARM64 agent deps →
# deploys all CDK stacks (foundation, data, agent, frontend) →
# SchemaMigrator Custom Resource creates 14+ tables idempotently →
# Cognito Callback URLs auto-registered to your CloudFront domain →
# /config.json deployed with your apiUrl, region, cognitoClientId, agentRuntimeArn.
```

```bash
# 4. Create the first user. Self sign-up is DISABLED by design, so without this
#    step the app deploys successfully and nobody can log in.
ENV=$(cd cdk && python3 -c "from config.settings import Settings; print(Settings.ENV)")
POOL=$(aws cloudformation describe-stacks --stack-name "dbops-$ENV-foundation" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)

aws cognito-idp admin-create-user --user-pool-id "$POOL" \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id "$POOL" \
  --username you@example.com --password 'ChangeMe123!' --permanent
```

Every user is an admin unless you add them to the `dbops-viewer` group, so no
extra step is needed to get full access. Omit `--permanent` if you would rather
be forced to set a new password at first login: the login page handles that
flow. Later users can be invited the same way, and roles are managed in the app
under Settings.

The final output of `./deploy.sh` shows your Web UI URL. Open it, log in with
the user you just created (the app has its own `/login` page, it does not use
the Cognito Hosted UI), then register a cluster from the Clusters page
(cross-account spoke roles are validated via STS AssumeRole at registration
time). Metrics appear after the first ETL cycle, which runs every 5 minutes.

#### One-time post-deploy: activate Bedrock cost-allocation tags

CDK creates six Application Inference Profiles tagged with `Application=DBOps`,
`Environment={env}`, `CostCategory=DBOps`. To populate the `/cost` page with
real spend you must activate the tag in your billing preferences once:

1. Open the [Cost allocation tags console](https://console.aws.amazon.com/billing/home#/preferences/tags)
2. Find **Application** under "User-defined cost allocation tags"
3. Select it → click **Activate**
4. Wait ~24h. Cost Explorer is not retroactive, but new chat invocations are
   immediately attributed to the profile from then on.

The `/cost` page's **Aurora / RDS** tab works out of the box (RDS total +
usage-type breakdown). **Per-cluster** Aurora cost is opt-in: apply a
cost-allocation tag to your Aurora clusters (e.g. `dbops:cluster=<id>`) and
activate that tag key in the same console. Until then the per-cluster panel
shows a "not available" notice instead of fabricated numbers.

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

#### Optional post-deploy: Ticketing (Jira / ServiceNow / …)

DBOps can file a ticket when an agent task completes. It ships as an inert
integration seam — off by default, toggled per-deployment in the web UI under
**Configure → Settings** (`TICKETING_PROVIDER`). Wiring your team's provider is
a few lines: see [docs/ticketing-integration.md](docs/ticketing-integration.md).

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

   Add `EnableProvisioningWrites=true` to the parameter overrides only if you
   want DBOps to CREATE resources in that account (restore a cluster from a
   snapshot or to a point in time, add a reader instance, create a custom cluster
   endpoint). Those actions cannot be restricted by the `ManagedBy=dbops` tag,
   because IAM evaluates the tag against the resource being acted on and the new
   resource does not exist yet, so they are opt-in. With the flag off, the
   restore and reader-scale-out tools return AccessDenied in that account.

2. Tag clusters for write access: `ManagedBy=dbops`

   Every write that acts on an existing resource is gated on this tag, across
   RDS/Aurora, DocumentDB (authorized under the `rds:` namespace), ElastiCache
   and DynamoDB. An untagged cluster is readable but not writable.

   Tag the **cluster parameter group** too. `rds:ModifyDBClusterParameterGroup`
   authorizes against the parameter group, not the cluster, so parameter tuning
   is denied if only the cluster carries the tag.

   Snapshot creation is the one exception: it is allowed without the tag. IAM
   evaluates `aws:ResourceTag` against every resource in the request, and the
   snapshot being created does not exist yet, so a tag condition would deny
   every snapshot rather than scope it.

3. Register in DBOps with the spoke role ARN

See `cdk/cross-account/README.md` for details.

## Project Structure

```
dbops/
├── cdk/                  # CDK infrastructure (4 stacks)
├── agent/                # Strands Agent + Dockerfile
├── mcp-servers/          # 4 Custom MCP servers (64 tools, incl. snapshot/restore + request_approval + query_activity_audit)
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

Recharts series/grid/axis/tooltip 색상은 inline SVG attr로 주입되어 CSS override가 닿지 않습니다. 모든 chart 컴포넌트는 `frontend/src/lib/use-chart-colors.ts` 의 `useChartColors()` 훅에서 amber/sky/emerald/rose **+ grid/axis/tooltipBg/tooltipBorder/tooltipText** 토큰을 가져와야 합니다. WCAG AA(4.5:1) 목표.

### 채팅에서 "승인됐어"라고 말했는데도 agent가 `approval_denied` 를 반환

서버측 approval guard가 켜진 이후로는 `approved=true`만으로는 부족하고, `approval_id` (request_approval 이 돌려준 UUID) 까지 같은 도구 호출에 함께 넘겨야 합니다. agent의 system prompt가 이 흐름을 알지만, 너무 오래된 모델/낮은 reasoning depth에서는 빠뜨릴 수 있으니 그럴 때는 `agent/prompts/system_prompt.py` 가이드라인 강화 + agent 스택 재배포. 또한 승인은 발급 후 30분 안에 사용해야 합니다 — 지났으면 `request_approval`을 다시 호출.

### `/reports` 에서 NL summary 대신 단순 템플릿 문장이 표시됨

`ReportGenerator` Lambda가 Bedrock invoke에 실패해서 결정적 fallback이 발동된 경우입니다. CloudWatch Logs에서 `[report_generator] Bedrock summary failed` 메시지 확인. 흔한 원인은 (1) `bedrock:InvokeModel` IAM 권한 누락 — 이 버전부터는 `data_stack`에 자동 부여, (2) `REPORT_SUMMARY_MODEL_ID` env가 가리키는 inference profile 미존재 — settings.py에서 모델 ID 확인. 데이터 자체(JSONB `data` 컬럼)는 정상 저장되니 UI의 카드/슬로우 쿼리 패널은 그대로 표시됩니다.

## Documentation

- Design Spec: `docs/superpowers/specs/2026-05-08-dbops-design.md`
- Implementation Plans: `docs/superpowers/plans/`
- Kiro Specs: `.kiro/specs/`
- Cedar Policies: `cdk/policies/`
- Cross-Account: `cdk/cross-account/README.md`

## License

Private — All rights reserved
