# DBOps: AI-Powered Database Operations Platform

[English](README.md) | **한국어**

AI 기반 종합 데이터베이스 운영 플랫폼. 자연어 대화로 Amazon Aurora(PostgreSQL·MySQL), RDS for MySQL·SQL Server(비-Aurora 독립형 인스턴스), DocumentDB, DynamoDB, ElastiCache(Redis/Valkey/Memcached)의 성능 분석, 장애 진단, 운영 자동화, 시뮬레이션을 수행합니다.

## Features

DBA를 위한 풀스택 운영 플랫폼입니다. **대화로 진단하고, 안전하게 실행하고, fleet 전체를 모니터링**합니다.
아래는 영역별 핵심 기능이며, 각 항목의 `/path`는 해당 Web UI 페이지입니다.

<details open>
<summary><b>🤖 AI & 대화</b></summary>

- **AI Chat** (`/chat`): 자연어로 성능 분석·장애 진단·운영 작업 요청. AWS MCP Server(SigV4)로 **공식 AWS/Aurora 문서를 근거로 인용**해 답변
- **Ask the Fleet** (`/ask`): "CPU 80% 넘은 클러스터 보여줘" 같은 자연어 fleet 조회. NL→filter compiler + saved views
- **AI Runbooks** (`/runbooks`): 채팅 진단/처방을 마크다운 playbook으로 저장·검색·재사용
- **Cross-Device Chat Sessions**: 대화가 DynamoDB에 영속화돼 다른 기기/브라우저에서 이어쓰기 (1.5s debounced sync + offline 캐시 + 90-day TTL)
- **Agent Memory Inspector** (`/preferences`): AgentCore Memory의 preferences/facts 조회·삭제. Cognito sub 기반 namespace로 cross-user read 차단

</details>

<details>
<summary><b>📊 성능 & 분석</b></summary>

- **Performance Analysis**: Slow query 분석, EXPLAIN plan tree + anti-pattern 자동 검출, 인덱스 추천, 이상 탐지
- **Schema Lineage** (`/schema`): `pg_constraint` 라이브 introspection으로 FK 관계 그래프 시각화
- **Replication Topology** (Dashboard): Writer/Readers + 인스턴스별 AuroraReplicaLag, promotion tier, multi-AZ
- **Redundant Indexes** (Dashboard): prefix-covered / 완전 중복 / unused 인덱스 자동 검출
- **Capacity Forecasting**: Storage/Connections/AAS 30·60·90일 선형 회귀 예측 + 임계 도달 시점
- **PG Log Insights** + **Keyword Search** (`/dashboard`): CloudWatch Logs Insights를 카테고리별로 묶어 조회, 검색어 AND 조인 + regex 살균
- **Saved Query Library** (Query Lab): 자주 쓰는 SQL 저장·태깅·cross-device 로드
- **MySQL Dashboard Parity**: Schema/Indexes/Log Insights 모두 Aurora MySQL 지원

</details>

<details>
<summary><b>📈 모니터링 & 알림</b></summary>

- **Monitoring Dashboard** (`/dashboard`): 실시간 클러스터 상태, 메트릭 시각화, Health Score
- **Fleet Overview** (`/fleet`): 전체 클러스터 한눈에. ETL 신선도 배지(fresh/stale/no_data) 포함
- **SLO Tracker** (`/slo`): 가용성 + p-mean 쿼리 지연 SLO 실측 + 에러 버짓 burn-down
- **Compound Alert Rules** (`/alerts`): 단일 threshold + AND/OR DSL(per-operand window/agg). DBA 프리셋 6종 + Slack 양방향 Ack
- **Alert Impact**: 알람 ±5min의 슬로우 쿼리·동시 이벤트·동시 알람을 인라인 패널로 (사고 triage)
- **Cost Anomaly Detection** (`/cost`): Bedrock 일별 사용액 spike를 z-score + 절대차 + 상대비 triple gate로 감지

</details>

<details>
<summary><b>🔧 운영 & 안전장치</b></summary>

- **Operations Automation**: 파라미터 변경, DDL 실행, 스케일링, **스냅샷·복원** (전부 Human-in-the-loop 승인)
- **Approval Guard**: 모든 write tool이 서버측에서 DDB approval row를 검증. agent가 `approved=true`를 임의로 못 켜고, `approval_id`(DBA가 `/approvals`에서 승인 시 발급) + cluster/action_type/30분 윈도우/atomic consume까지 강제
- **Simulation UI** (`/simulator`): 업그레이드(호환성+method matrix+ordered plan)·파라미터·ACU 비용·DDL 영향을 채팅 없이 즉시 추정

</details>

<details>
<summary><b>🚨 인시던트 & 감사</b></summary>

- **Incident Diagnosis**: RCA, 시그널 상관 분석, 타임라인 재구성
- **Incident Timeline** (`/timeline`): 한 cluster의 모든 신호(알람/RDS 이벤트/스키마 변경/proactive/Slack ack/실행된 쓰기)를 시간축 한 줄에, 카테고리 칩 필터
- **DBOps Activity Log** (`/activity`): 누가 무엇을 요청/승인/실행했는지 시간순 기록 (컴플라이언스 감사 + 사후 회고). `query_activity_audit` MCP 도구로 채팅에서도 질의 가능
- **Daily Operations Report** (`/reports`): `report_generator` Lambda가 매일 자정 24h 메트릭을 집계 + Bedrock Claude로 한국어 요약 (실패 시 템플릿 fallback)

</details>

<details>
<summary><b>🏢 플랫폼 & 멀티계정</b></summary>

- **Cross-Account**: Hub-Spoke IAM 패턴으로 여러 AWS 계정의 Aurora 통합 관리
- **Cluster Registration Wizard** (`/clusters`): same/cross-account 모드 토글, "연결만 테스트" 3-step pre-flight (STS AssumeRole + DescribeDBClusters + master secret)
- **Schema Migration Auto-Trigger**: `cdk deploy` 시 SQL 디렉터리 SHA-256 해시를 schema_version에 주입해 변경 시 자동 마이그레이션

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

- **Custom 도구는 Gateway 경유**(write 승인은 tool-level `approval_guard`가 강제하며, Cedar Policy Engine은 게이트웨이에 LOG_ONLY로 바인딩된 방어 심층입니다), **공식 AWS 문서는 AWS MCP Server에 SigV4로 직접** 호출: 읽기 전용 문서 도구만 노출합니다
- **Dashboard 데이터는 사전 수집 캐시에서** 읽습니다. 실시간 렌더링 중 AWS API를 직접 호출하지 않습니다 (라이브 패널만 예외: topology/backup)

- **Single Agent + Gateway**: 단일 AgentCore Runtime + Gateway MCP로 지연/토큰 최적화
- **CDK-First**: 모든 인프라는 CDK로만 관리. `cdk deploy --all`로 전체 배포
- **Human-in-the-loop**: 조회는 자동, 변경은 DBA 승인 필수 (tool-level `approval_guard` 강제: fail-closed·payload-hash 바인딩·single-use)

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

- AdministratorAccess 권한을 가진 AWS 계정
- Node.js 20+, Python 3.10+
- AWS CDK CLI (`npm install -g aws-cdk`)
- 아래 **두 가지 모두**에 대한 **Bedrock 모델 액세스**:
  - Claude 텍스트 모델. 사용 리전에 맞는 cross-region inference profile
    (`apac.` / `us.` / `eu.` / `global.` 접두사)로 활성화합니다. 이 값이 `AGENT_MODEL_ID`입니다.
  - **Anthropic "use case details" 양식. 계정당 한 번 제출합니다.** 모델 액세스
    활성화와는 별개 단계이고 놓치기 쉽습니다. 제출하지 않으면 에이전트가 매 턴
    `ResourceNotFoundException: Model use case details have not been
submitted for this account` 로 실패하는데, 양식 누락이 아니라 모델 누락처럼
    읽힙니다. Bedrock 콘솔에서 제출하고 약 15분 기다리세요. 2026-08-12 측정:
    새 계정에서는 `apac.anthropic.claude-sonnet-4-20250514-v1:0` 에 대한
    `Converse` 와 `InvokeModel` 이 모두 이 오류를 반환했고, 양식을 제출한 계정은
    동일한 호출에 정상 응답했습니다.
  - **Amazon Titan Text Embeddings V2** (`amazon.titan-embed-text-v2:0`). 시맨틱
    인시던트 검색이 이 모델로 임베딩해 pgvector에 넣습니다(`incident_embeddings`
    collector와 `find_similar_incidents` 도구). 액세스가 없으면 이 두 경로만
    키워드 매칭으로 폴백하고 나머지는 정상으로 보입니다.
- **Bedrock AgentCore**(Runtime, Gateway, Memory)를 사용할 수 있는 리전. agent
  스택은 이것 없이는 배포되지 않으며, 모든 리전에서 제공되지는 않습니다.

### Running cost

프리 티어로 운영되는 구성이 아닙니다. 아무도 로그인하지 않아도 과금되는 구성 요소가 둘 있습니다.

| always-on                                  | measured                |
| ------------------------------------------ | ----------------------- |
| Aurora Serverless v2 캐시, 최소 0.5 ACU    | 월 약 $70               |
| NAT Gateway 1개 (프라이빗 Lambda 이그레스) | 월 약 $37 (시간 요금만) |

즉 트래픽이 전혀 없어도 **월 약 $110**이고, 여기에 사용량이 더해집니다: Lambda(등록된
클러스터 수 × 5분 주기 ETL에 비례), Bedrock 토큰, CloudFront, DynamoDB, S3.

규모 감각을 위해: 이 프로젝트의 dev 계정을 `Application=DBOps` 비용 할당 태그로
2026-08-11까지 30일간 측정한 값은 $485였습니다. 이를 그대로 자기 청구서로 보면 안
됩니다. 대부분은 모니터링 대상인 데모 TARGET 데이터베이스 비용이고, 그중 프로비저닝
`db.r6g.large` Aurora 한 대만으로 월 약 $180입니다. 등록 클러스터 11개 기준으로
플랫폼 자체의 몫은 월 $200 정도였습니다.

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

`dbops-viewer` 그룹에 넣지 않은 사용자는 모두 admin이므로, 전체 권한을 받기 위한
추가 작업은 없습니다. 첫 로그인 때 새 비밀번호를 강제로 설정하게 하려면
`--permanent`를 생략하세요. 로그인 페이지가 그 흐름을 처리합니다. 이후 사용자도 같은
방식으로 초대할 수 있고, 역할은 앱의 Settings에서 관리합니다.

`./deploy.sh`의 마지막 출력에 Web UI URL이 표시됩니다. 접속해서 방금 만든 사용자로
로그인하고(앱은 자체 `/login` 페이지를 쓰며 Cognito Hosted UI를 사용하지 않습니다),
Clusters 페이지에서 클러스터를 등록하세요(cross-account spoke role은 등록 시점에 STS
AssumeRole로 검증됩니다). 메트릭은 첫 ETL 주기가 돌고 나서 표시되며, ETL은 5분마다
실행됩니다.

#### One-time post-deploy: activate Bedrock cost-allocation tags

CDK는 `Application=DBOps`, `Environment={env}`, `CostCategory=DBOps` 태그가 붙은
Application Inference Profile 6개를 생성합니다. `/cost` 페이지에 실제 지출을 채우려면
빌링 환경설정에서 태그를 한 번 활성화해야 합니다.

1. [Cost allocation tags 콘솔](https://console.aws.amazon.com/billing/home#/preferences/tags) 열기
2. "User-defined cost allocation tags"에서 **Application** 찾기
3. 선택 후 **Activate** 클릭
4. 약 24시간 대기. Cost Explorer는 소급 적용되지 않지만, 활성화 이후의 새 chat
   호출은 즉시 해당 프로파일로 귀속됩니다.

`/cost` 페이지의 **Aurora / RDS** 탭은 별도 설정 없이 동작합니다(RDS 합계 +
usage-type 분해). **클러스터별** Aurora 비용은 옵트인입니다. Aurora 클러스터에 비용
할당 태그(예: `dbops:cluster=<id>`)를 붙이고 같은 콘솔에서 그 태그 키를 활성화하세요.
그전까지 클러스터별 패널은 숫자를 만들어내지 않고 "not available" 안내를 표시합니다.

#### Optional post-deploy: Slack two-way Ack

Outbound Slack 알림 메시지의 "✓ Ack" 버튼이 동작하려면 Slack 앱 측 설정이 한 번 필요합니다. **Alerts 페이지의 "Slack 양방향 Ack 설정" 섹션에서 4단계 가이드 + endpoint URL을 자동으로 받을 수 있습니다.** (PageHeader 아래의 "셋업 가이드 열기" 버튼).

요약:

1. [api.slack.com/apps](https://api.slack.com/apps?new_app=1)에서 새 Slack 앱 생성
2. **Basic Information → Signing Secret** 복사 → `cdk/config/settings.py`의 `SLACK_SIGNING_SECRET`에 붙여넣기
3. **Interactivity & Shortcuts** 활성화 → Request URL에 `{API_GATEWAY_URL}/api/slack/interactive` 입력
4. **Incoming Webhooks** 활성화 → 채널 webhook URL을 Alerts 페이지 Subscribers에 `slack-webhook` 프로토콜로 등록 → `cdk deploy dbops-dev-agent` 한번 더

Signing Secret이 비어 있어도 outbound Slack 메시지(읽기) + 모든 다른 기능은 정상 작동합니다. 양방향 ack만 비활성화됩니다.

#### Optional post-deploy hardening

```bash
# Tighten CORS: by default Lambdas echo request Origin (dev-safe).
# Set ALLOWED_ORIGINS on dashboard/alerts Lambdas to your CloudFront domain
# in production. (Auto-injection would create a cyclic CFN dependency.)

# Configure AgentCore (post-deploy)
# npm install -g @aws/agentcore
# agentcore create --defaults
# agentcore deploy

# 8. Register your first cluster
# Open the Web UI → Clusters → + Register
```

> **Cognito Callback URL은 자동 등록됩니다**: frontend 스택이 AWS Custom
> Resource로 배포 시점에 `<DistributionUrl>/callback`(과 localhost)을 User Pool
> 클라이언트에 추가합니다. 수동 Cognito 설정은 필요하지 않습니다.
>
> **런타임 설정**: 프런트엔드는 부팅 시 `/config.json`을 가져오므로 빌드된 정적
> 번들이 이식 가능합니다. CDK가 배포 시점의 라이브 스택 출력에서 값을 해석해
> `apiUrl`, `frontendUrl`, `region`, `cognitoDomain`, `cognitoClientId`,
> `agentRuntimeArn`을 config 객체에 기록합니다.

#### Optional post-deploy: Ticketing (Jira / ServiceNow / …)

DBOps는 agent 작업이 끝났을 때 티켓을 생성할 수 있습니다. 기본은 비활성인 연동
접점(inert integration seam)으로 배포되며, 배포별로 웹 UI의 **Configure →
Settings**(`TICKETING_PROVIDER`)에서 토글합니다. 팀에서 쓰는 provider를 붙이는 작업은
몇 줄이면 됩니다. [docs/ticketing-integration.md](docs/ticketing-integration.md)를
참고하세요.

### Cluster Credential Setup (production-recommended)

DBOps는 RDS Data API로 클러스터 모니터링 데이터를 읽습니다. 기본값은 각 Aurora
클러스터와 함께 발견되는 master secret이며, 별도 설정 없이 동작하지만 DBOps에 admin
사용자와 동일한 blast radius를 부여합니다.

프로덕션에서는 클러스터마다 전용 `dbops_readonly` 역할을 만들고 **DBOps 명명 규칙**에
맞춰 Secrets Manager에 자격 증명을 등록하세요. 그러면 일괄 Discover가 이를 찾아
자동으로 연결하므로 ARN을 직접 입력할 필요가 없습니다.

```
secret name: dbops/<cluster_id>/readonly
secret JSON: {"username":"dbops_readonly","password":"..."}
```

Clusters 페이지의 **📋 Setup guide** 버튼을 누르면 PostgreSQL과 MySQL 각각의 SQL +
AWS CLI 스니펫을 단계별로 안내합니다. 요약하면 다음과 같습니다.

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

그다음 자격 증명을 등록합니다.

```bash
aws secretsmanager create-secret \
  --region <region> \
  --name "dbops/<cluster_id>/readonly" \
  --secret-string '{"username":"dbops_readonly","password":"<password>"}'
```

이후 Discover 테이블은 클러스터별로 다음 세 배지 중 하나를 표시합니다.

- `✓ convention`: 전용 사용자를 찾아 자동 연결됨 (권장)
- `⚠ master fallback`: master secret 사용 중. 동작하지만 권한을 좁히는 편이 좋습니다
- `✗ missing`: 사용 가능한 secret 없음. 활성화 전에 설정이 필요합니다

### Cross-Account Setup

다른 AWS 계정의 Aurora 클러스터를 관리하려면:

1. 대상 계정마다 spoke role을 배포합니다.

   ```bash
   aws cloudformation deploy \
     --template-file cdk/cross-account/spoke-role-template.yaml \
     --stack-name dbops-spoke-role \
     --parameter-overrides HubAccountId=<HUB_ACCOUNT_ID> \
     --capabilities CAPABILITY_NAMED_IAM
   ```

   해당 계정에서 DBOps가 리소스를 CREATE 하도록 하려면(스냅샷 또는 특정 시점으로
   클러스터 복원, reader 인스턴스 추가, custom cluster endpoint 생성) 파라미터
   오버라이드에 `EnableProvisioningWrites=true`를 추가하세요. 이 동작들은
   `ManagedBy=dbops` 태그로 제한할 수 없습니다. IAM은 태그를 작업 대상 리소스에 대해
   평가하는데 새 리소스는 아직 존재하지 않기 때문이며, 그래서 옵트인입니다. 플래그를
   끈 상태에서는 restore와 reader scale-out 도구가 그 계정에서 AccessDenied를
   반환합니다.

2. write 액세스를 위해 클러스터에 태그를 붙입니다: `ManagedBy=dbops`

   기존 리소스를 대상으로 하는 모든 write는 이 태그로 게이트됩니다. RDS/Aurora,
   DocumentDB(`rds:` 네임스페이스에서 인가), ElastiCache, DynamoDB 전부
   해당합니다. 태그가 없는 클러스터는 읽기는 되지만 쓰기는 되지 않습니다.

   **cluster parameter group에도** 태그를 붙이세요.
   `rds:ModifyDBClusterParameterGroup`은 클러스터가 아니라 파라미터 그룹을 기준으로
   인가하므로, 클러스터에만 태그가 있으면 파라미터 튜닝이 거부됩니다.

   스냅샷 생성은 유일한 예외로, 태그 없이도 허용됩니다. IAM은 요청에 포함된 모든
   리소스에 대해 `aws:ResourceTag`를 평가하는데 생성 중인 스냅샷은 아직 존재하지
   않으므로, 태그 조건은 범위를 좁히는 게 아니라 모든 스냅샷을 거부하게 됩니다.

3. spoke role ARN으로 DBOps에 등록합니다.

자세한 내용은 `cdk/cross-account/README.md`를 참고하세요.

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

클론당 한 번만 설치하면 이후 모든 commit / push가 자동으로 검사됩니다.

```bash
# Dev deps (pytest, ruff, pre-commit, CDK synth deps)
pip install -r requirements-dev.txt

# Install pre-commit hooks (ruff + prettier + tsc --noEmit on TS changes)
pre-commit install
```

GitHub Actions(`.github/workflows/ci.yml`)가 모든 push/PR에서 동일한 검사를 다시
실행합니다. 병렬 job 3개입니다.

- **python**: `ruff check .` + `pytest tests/unit`
- **cdk**: `cdk synth` smoke + 4-stack 스냅샷 테스트
- **frontend**: `tsc --noEmit` + `next build`

Claude Code hooks(`.claude/hooks/`)는 어시스턴트와 작업할 때 구조적 게이트 두 개를
더합니다.

- **`pre-commit-review.sh`**: code-reviewer 서브에이전트가 돌기 전까지 `git commit`을
  막습니다. 사소한 diff는 커밋 메시지에 `[skip-review]`를 넣어 우회합니다.
- **`stop-session-memory.sh`**: 세션 종료 시 마지막 체크포인트 이후의 커밋을 보여주고,
  다음 세션이 깔끔하게 이어지도록 프로젝트 메모리 노트를 남기라고 어시스턴트에게
  상기시킵니다.

## Troubleshooting

### The Fleet page shows a cluster I never registered

PG 캐시(`cluster_meta`)에 과거 ETL이 남긴 행이 있고 DDB 레지스트리에는 없는 경우. 현재 build는 `_multi_cluster_overview`가 DDB와 자동 교차 검증하므로 신규 deploy 이후엔 안 보입니다. 기존 캐시 청소가 필요하면 `cluster_meta` + per-cluster 테이블에서 해당 `cluster_id` DELETE.

### The Cost page shows only $0

`Application` cost allocation 태그가 billing console에서 활성화되지 않은 경우. Quick Start의 "post-deploy: activate Bedrock cost-allocation tags" 단계 확인. 활성화 후에도 과거 비용은 backfill되지 않고 그 시점 이후 호출분만 집계됩니다.

### Clicking the Slack "✓ Ack" button returns "SLACK_SIGNING_SECRET not configured"

`cdk/config/settings.py`의 `SLACK_SIGNING_SECRET`이 비어 있음. 위 "Slack 양방향 Ack" 가이드의 4단계 수행 + agent 스택 재배포 필요.

### The Schema lineage / Replication topology panels say MySQL is not supported in v1

PostgreSQL 전용 기능들입니다. MySQL 클러스터에서는 friendly 안내가 표시되며 다른 패널은 정상 동작합니다.

### Bedrock responses are unusually slow

Cold start 또는 region capacity 이슈. CloudWatch에서 AgentCore Runtime 로그 확인. AGENT_MODEL_ID를 가벼운 모델로 임시 전환해서 비교: `settings.py`의 `AGENT_MODEL_ID`를 Haiku로.

### Some text is invisible in light mode

Recharts series/grid/axis/tooltip 색상은 inline SVG attr로 주입되어 CSS override가 닿지 않습니다. 모든 chart 컴포넌트는 `frontend/src/lib/use-chart-colors.ts` 의 `useChartColors()` 훅에서 amber/sky/emerald/rose **+ grid/axis/tooltipBg/tooltipBorder/tooltipText** 토큰을 가져와야 합니다. WCAG AA(4.5:1) 목표.

### The agent returns `approval_denied` even though I said "approved" in chat

서버측 approval guard가 켜진 이후로는 `approved=true`만으로는 부족하고, `approval_id` (request_approval 이 돌려준 UUID) 까지 같은 도구 호출에 함께 넘겨야 합니다. agent의 system prompt가 이 흐름을 알지만, 너무 오래된 모델/낮은 reasoning depth에서는 빠뜨릴 수 있으니 그럴 때는 `agent/prompts/system_prompt.py` 가이드라인 강화 + agent 스택 재배포. 또한 승인은 발급 후 30분 안에 사용해야 합니다. 지났으면 `request_approval`을 다시 호출하세요.

### `/reports` shows plain template sentences instead of the NL summary

`ReportGenerator` Lambda가 Bedrock invoke에 실패해서 결정적 fallback이 발동된 경우입니다. CloudWatch Logs에서 `[report_generator] Bedrock summary failed` 메시지 확인. 흔한 원인은 (1) `bedrock:InvokeModel` IAM 권한 누락(이 버전부터는 `data_stack`에서 자동 부여), (2) `REPORT_SUMMARY_MODEL_ID` env가 가리키는 inference profile 미존재(settings.py에서 모델 ID 확인). 데이터 자체(JSONB `data` 컬럼)는 정상 저장되니 UI의 카드/슬로우 쿼리 패널은 그대로 표시됩니다.

## Documentation

- 설계 스펙: `docs/superpowers/specs/2026-05-08-dbops-design.md`
- 구현 계획: `docs/superpowers/plans/`
- Kiro 스펙: `.kiro/specs/`
- Cedar 정책: `cdk/policies/`
- Cross-Account: `cdk/cross-account/README.md`

## License

비공개(Private). All rights reserved
