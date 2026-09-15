# DBOps: AI-Powered Database Operations Platform

**English** | [한국어](README.ko.md)

An AI-powered platform for end-to-end database operations. Run performance analysis, incident diagnosis, operational automation, and simulation across Amazon Aurora (PostgreSQL and MySQL), RDS for MySQL and SQL Server (standalone, non-Aurora instances), DocumentDB, DynamoDB, and ElastiCache (Redis/Valkey/Memcached), all through natural-language conversation.

## Features

A full-stack operations platform for DBAs: **diagnose by talking to it, execute safely, monitor the whole fleet**.
The sections below group the core capabilities by area, and the `/path` on each item is the Web UI page it lives on.

<details open>
<summary><b>🤖 AI & Conversation</b></summary>

- **AI Chat** (`/chat`): ask for performance analysis, incident diagnosis, and operational work in plain language. Answers **cite the official AWS/Aurora documentation as their evidence** via the AWS MCP Server (SigV4)
- **Ask the Fleet** (`/ask`): natural-language fleet queries such as "show me the clusters above 80% CPU". NL→filter compiler + saved views
- **AI Runbooks** (`/runbooks`): save, search, and reuse a chat diagnosis or prescription as a markdown playbook
- **Cross-Device Chat Sessions**: conversations persist in DynamoDB, so you can pick one up on another device or browser (1.5s debounced sync + offline cache + 90-day TTL)
- **Agent Memory Inspector** (`/preferences`): read and delete the preferences/facts held in AgentCore Memory. Namespaces are keyed on the Cognito sub, which blocks cross-user reads

</details>

<details>
<summary><b>📊 Performance & Analysis</b></summary>

- **Performance Analysis**: slow query analysis, EXPLAIN plan tree with automatic anti-pattern detection, index recommendations, anomaly detection
- **Schema Lineage** (`/schema`): FK relationship graph visualized from live `pg_constraint` introspection
- **Replication Topology** (Dashboard): writer/readers plus per-instance AuroraReplicaLag, promotion tier, multi-AZ
- **Redundant Indexes** (Dashboard): automatic detection of prefix-covered, fully duplicate, and unused indexes
- **Capacity Forecasting**: 30/60/90-day linear-regression forecasts for storage, connections, and AAS, plus the point each one crosses its threshold
- **PG Log Insights** + **Keyword Search** (`/dashboard`): CloudWatch Logs Insights grouped by category, with search terms AND-joined and regex sanitized
- **Saved Query Library** (Query Lab): store, tag, and cross-device load the SQL you run often
- **MySQL Dashboard Parity**: Schema, Indexes, and Log Insights all support Aurora MySQL

</details>

<details>
<summary><b>📈 Monitoring & Alerting</b></summary>

- **Monitoring Dashboard** (`/dashboard`): live cluster status, metric visualization, Health Score
- **Fleet Overview** (`/fleet`): every cluster at a glance, with ETL freshness badges (fresh/stale/no_data)
- **SLO Tracker** (`/slo`): measured availability and p-mean query-latency SLOs with error-budget burn-down
- **Compound Alert Rules** (`/alerts`): a single threshold or an AND/OR DSL (per-operand window/agg). Six DBA presets + two-way Slack Ack
- **Alert Impact**: the slow queries, concurrent events, and concurrent alarms within ±5min of an alarm, in an inline panel (incident triage)
- **Cost Anomaly Detection** (`/cost`): catches spikes in daily Bedrock spend through a triple gate of z-score, absolute delta, and relative ratio

</details>

<details>
<summary><b>🔧 Operations & Safety</b></summary>

- **Operations Automation**: parameter changes, DDL execution, scaling, **snapshot and restore** (all human-in-the-loop approved)
- **Approval Guard**: every write tool validates a DDB approval row server-side. The agent cannot flip `approved=true` by itself: it must also pass an `approval_id` (issued when a DBA approves in `/approvals`), and the cluster, the action_type, a 30-minute window, and an atomic consume are all enforced
- **Simulation UI** (`/simulator`): estimate upgrades (compatibility + method matrix + ordered plan), parameter changes, ACU cost, and DDL impact directly, without going through chat

</details>

<details>
<summary><b>🚨 Incidents & Audit</b></summary>

- **Incident Diagnosis**: RCA, signal correlation, timeline reconstruction
- **Incident Timeline** (`/timeline`): every signal for one cluster (alarms, RDS events, schema changes, proactive findings, Slack acks, executed writes) on a single time axis, with category chip filters
- **DBOps Activity Log** (`/activity`): a chronological record of who requested, approved, and executed what (compliance audit + post-incident review). Also queryable from chat through the `query_activity_audit` MCP tool
- **Daily Operations Report** (`/reports`): the `report_generator` Lambda aggregates 24h of metrics at midnight and summarizes them in Korean with Bedrock Claude (template fallback on failure)

</details>

<details>
<summary><b>🏢 Platform & Multi-Account</b></summary>

- **Cross-Account**: manage Aurora across several AWS accounts from one place with a hub-spoke IAM pattern
- **Cluster Registration Wizard** (`/clusters`): same/cross-account mode toggle, plus a connection-test-only 3-step pre-flight (STS AssumeRole + DescribeDBClusters + master secret)
- **Schema Migration Auto-Trigger**: `cdk deploy` injects a SHA-256 hash of the SQL directory into schema_version, so a change migrates automatically

</details>

## Architecture

```
Web UI (Next.js, static) ──SSE──▶ AgentCore Runtime (Strands Agent)
                                    │                        │
                          AgentCore Gateway          AWS MCP Server
                          (Cedar Policy)             (SigV4, official AWS docs)
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼            ▼              ▼               ▼
         Performance   Incident      Operations       Simulation
           MCP          MCP            MCP               MCP
                       (4 custom MCP servers, 64 tools)
                                    │
                                    ▼
                  Aurora PG Cache ◀── Data Collection Pipeline
                  (hot cache)         (ETL, Event Processor, Report, Monitor)
```

- **Custom tools go through the Gateway** (write approval is enforced by the tool-level `approval_guard`; the Cedar Policy Engine is bound at the Gateway in LOG_ONLY as defense in depth), **official AWS docs go straight to the AWS MCP Server over SigV4**, which exposes read-only documentation tools and nothing else
- **Dashboard data comes from the pre-collected cache**: no AWS API is called directly during rendering (the live panels are the only exception: topology and backup)

- **Single Agent + Gateway**: one AgentCore Runtime plus one Gateway MCP, tuned for latency and token count
- **CDK-First**: all infrastructure is managed through CDK only. `cdk deploy --all` deploys the whole thing
- **Human-in-the-loop**: reads are automatic, changes require DBA approval (enforced by the tool-level `approval_guard`: fail-closed, payload-hash bound, single-use)

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
  - **the Anthropic "use case details" form, submitted once per account.** This is a
    SEPARATE step from enabling model access and it is easy to miss: the agent fails
    every turn with `ResourceNotFoundException: Model use case details have not been
submitted for this account`, which reads like a missing model rather than a missing
    form. Submit it in the Bedrock console and allow ~15 minutes. Measured 2026-08-12:
    a fresh account returned that error from both `Converse` and `InvokeModel` on
    `apac.anthropic.claude-sonnet-4-20250514-v1:0` while an account that had submitted
    the form answered normally with the identical call.
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

#### Optional post-deploy: Slack two-way Ack

The "✓ Ack" button on outbound Slack alert messages needs one setup pass on the Slack app side. **The Alerts page has a Slack two-way Ack setup section that hands you the 4-step guide plus your endpoint URL automatically** (the setup-guide button under the PageHeader).

In short:

1. Create a new Slack app at [api.slack.com/apps](https://api.slack.com/apps?new_app=1)
2. Copy **Basic Information → Signing Secret** and paste it into `SLACK_SIGNING_SECRET` in `cdk/config/settings.py`
3. Enable **Interactivity & Shortcuts** and set the Request URL to `{API_GATEWAY_URL}/api/slack/interactive`
4. Enable **Incoming Webhooks**, register the channel webhook URL under Subscribers on the Alerts page with the `slack-webhook` protocol, then run `cdk deploy dbops-dev-agent` once more

Outbound Slack messages (the read direction) and every other feature work normally with an empty Signing Secret. Only two-way ack is disabled.

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
integration seam: off by default, toggled per-deployment in the web UI under
**Configure → Settings** (`TICKETING_PROVIDER`). Wiring your team's provider is
a few lines: see [docs/ticketing-integration.md](docs/ticketing-integration.md).

### Cluster Credential Setup (production-recommended)

DBOps reads cluster monitoring data over the RDS Data API. By default the
master secret discovered alongside each Aurora cluster is used, which works
out-of-the-box but grants DBOps the same blast radius as the admin user.

For production, create a dedicated `dbops_readonly` role on each cluster and
register its credentials in Secrets Manager using the **DBOps naming
convention**: bulk Discover finds and attaches it automatically, no manual
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

- `✓ convention`: dedicated user found and auto-attached (recommended)
- `⚠ master fallback`: using master secret; works but should be tightened
- `✗ missing`: no usable secret; needs setup before activation

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

Install once per clone, and every commit / push is auto-checked from then on:

```bash
# Dev deps (pytest, ruff, pre-commit, CDK synth deps)
pip install -r requirements-dev.txt

# Install pre-commit hooks (ruff + prettier + tsc --noEmit on TS changes)
pre-commit install
```

GitHub Actions (`.github/workflows/ci.yml`) re-runs the same checks on
every push/PR, in three parallel jobs:

- **python**: `ruff check .` + `pytest tests/unit`
- **cdk**: `cdk synth` smoke + 4-stack snapshot test
- **frontend**: `tsc --noEmit` + `next build`

Claude Code hooks (`.claude/hooks/`) add two structural gates while
working with the assistant:

- **`pre-commit-review.sh`**: blocks `git commit` until the code-reviewer
  subagent runs. Bypass for trivial diffs by adding `[skip-review]` to the
  commit message.
- **`stop-session-memory.sh`**: at session end, surfaces commits since
  the last checkpoint and reminds the assistant to persist a project
  memory note so the next session resumes cleanly.

## Troubleshooting

### The Fleet page shows a cluster I never registered

The PG cache (`cluster_meta`) still holds rows left behind by an earlier ETL run while the DDB registry has none. In the current build `_multi_cluster_overview` cross-checks against DDB automatically, so this stops happening after a new deploy. To clean up an existing cache, DELETE that `cluster_id` from `cluster_meta` and the per-cluster tables.

### The Cost page shows only $0

The `Application` cost allocation tag has not been activated in the billing console. Check the "post-deploy: activate Bedrock cost-allocation tags" step in Quick Start. Even after activation, past cost is not backfilled: only invocations from that point forward are aggregated.

### Clicking the Slack "✓ Ack" button returns "SLACK_SIGNING_SECRET not configured"

`SLACK_SIGNING_SECRET` in `cdk/config/settings.py` is empty. Run step 4 of the "Slack two-way Ack" guide above and redeploy the agent stack.

### The Schema lineage / Replication topology panels say MySQL is not supported in v1

Both are PostgreSQL-only features. On a MySQL cluster you get a friendly notice there, and every other panel works normally.

### Bedrock responses are unusually slow

Cold start, or region capacity. Check the AgentCore Runtime logs in CloudWatch. To compare, temporarily point `AGENT_MODEL_ID` in `settings.py` at a lighter model such as Haiku.

### Some text is invisible in light mode

Recharts injects series/grid/axis/tooltip colors as inline SVG attributes, so a CSS override never reaches them. Every chart component must take amber/sky/emerald/rose **plus the grid/axis/tooltipBg/tooltipBorder/tooltipText tokens** from the `useChartColors()` hook in `frontend/src/lib/use-chart-colors.ts`. The target is WCAG AA (4.5:1).

### The agent returns `approval_denied` even though I said "approved" in chat

Since the server-side approval guard shipped, `approved=true` on its own is not enough: the same tool call must also carry the `approval_id` (the UUID `request_approval` returned). The agent's system prompt knows this flow, but a model that is too old, or one running at low reasoning depth, can drop it. When that happens, strengthen the guidelines in `agent/prompts/system_prompt.py` and redeploy the agent stack. An approval also has to be used within 30 minutes of being issued: once that window passes, call `request_approval` again.

### `/reports` shows plain template sentences instead of the NL summary

The `ReportGenerator` Lambda failed its Bedrock invoke and the deterministic fallback took over. Look for `[report_generator] Bedrock summary failed` in CloudWatch Logs. The common causes are (1) a missing `bedrock:InvokeModel` IAM permission, which `data_stack` grants automatically as of this version, and (2) the inference profile that the `REPORT_SUMMARY_MODEL_ID` env var points at does not exist, so check the model ID in settings.py. The data itself (the JSONB `data` column) is stored correctly, so the cards and slow-query panels in the UI still render.

## Documentation

- Design Spec: `docs/superpowers/specs/2026-05-08-dbops-design.md`
- Implementation Plans: `docs/superpowers/plans/`
- Kiro Specs: `.kiro/specs/`
- Cedar Policies: `cdk/policies/`
- Cross-Account: `cdk/cross-account/README.md`

## License

Private. All rights reserved
