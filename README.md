# DBOps — AI-Powered Database Operations Platform

AI 기반 종합 데이터베이스 운영 플랫폼. 자연어 대화로 Amazon Aurora MySQL/PostgreSQL 클러스터의 성능 분석, 장애 진단, 운영 자동화, 시뮬레이션을 수행합니다.

## Features

- **AI Chat** — 자연어로 DB 성능 분석, 장애 진단, 운영 작업 요청
- **Performance Analysis** — Slow query 분석, EXPLAIN 해석, 인덱스 추천, 이상 탐지
- **Incident Diagnosis** — RCA(Root Cause Analysis), 시그널 상관 분석, 타임라인 재구성
- **Operations Automation** — 파라미터 변경, DDL 실행, 스케일링 (Human-in-the-loop 승인)
- **Simulation** — 버전 업그레이드 영향 분석, 파라미터 변경 시뮬레이션, DDL 영향도 예측
- **Monitoring Dashboard** — 실시간 클러스터 상태, 메트릭 시각화, 프로액티브 알림
- **Cross-Account** — Hub-Spoke IAM 패턴으로 여러 AWS 계정의 Aurora 클러스터 통합 관리

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

## Documentation

- Design Spec: `docs/superpowers/specs/2026-05-08-dbops-design.md`
- Implementation Plans: `docs/superpowers/plans/`
- Kiro Specs: `.kiro/specs/`
- Cedar Policies: `cdk/policies/`
- Cross-Account: `cdk/cross-account/README.md`

## License

Private — All rights reserved
