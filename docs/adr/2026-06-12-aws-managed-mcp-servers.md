# ADR: Use of AWS-managed database MCP servers (Aurora / DynamoDB / DocumentDB)

- **Date**: 2026-06-12
- **Status**: Accepted
- **Context owner**: DBOps multi-engine program (specs #2 DocumentDB diagnosis, #3 DynamoDB diagnosis)
- **Verified via**: AWS docs (awslabs/mcp catalog, DynamoDB/DocDB MCP READMEs) + codebase
  (agent/server.py, agent_stack.py, approval_guard.py, cdk/policies/cedar/) + Codex
  adversarial review (verdict: SOFTEN — decision stands; tightened Option-B to require
  credential-level read-only enforcement, not just an MCP flag).

## Context

AWS publishes official open-source MCP servers (the `awslabs/mcp` catalog), including
database servers relevant to us:

| AWS MCP server                                          | What it does                                                                                                              |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Amazon Aurora PostgreSQL MCP                            | SQL operations **via RDS Data API**                                                                                       |
| Amazon Aurora MySQL MCP                                 | SQL operations **via RDS Data API**                                                                                       |
| Amazon DynamoDB MCP (`uvx awslabs.dynamodb-mcp-server`) | Full DynamoDB ops + table management; **local stdio**; local AWS credential chain; `DDB-MCP-READONLY=true` read-only flag |
| Amazon DocumentDB MCP                                   | MongoDB-compatible (Mongo wire protocol) document ops                                                                     |
| DynamoDB data-modeling MCP                              | Design-time key/index modeling assistant (not ops/monitoring)                                                             |

Common shape: **local stdio process**, **local AWS credential chain** (or a Mongo
connection string for DocDB), **write-capable by default**.

We already use one AWS-managed MCP server: the **AWS Documentation/Knowledge MCP**
(`agent/server.py:38-44`, `AWS_MCP_URL=https://aws-mcp.us-east-1.api.aws/mcp`), called
**directly via SigV4-signed JSON-RPC, read-only, OUTSIDE the Gateway** — exposing only
`search_aws_documentation` / `read_aws_documentation`. So a **read-only AWS MCP
connected directly (bypassing the Cedar/approval Gateway) is an established, sanctioned
pattern** here. The open question is whether to extend that pattern to **data-plane**
DB MCP servers.

Our custom tooling is **4 MCP Lambdas** (performance/incident/operations/simulation)
registered as AgentCore Gateway **MCP-Lambda targets** (`cdk/stacks/agent_stack.py:200`,
`McpLambdaTargetConfigurationProperty`, inline tool schema, `GATEWAY_IAM_ROLE`
credentials); the 5th "server" (knowledge) is the AWS-managed MCP above, not a Lambda.
Our value layer:

- **Cache-first**: dashboards/metrics read the pre-collected Aurora PG cache, never
  live AWS calls (AGENTS.md rule).
- **DBA diagnosis heuristics** computed over cache (param fitness, capacity forecast, RCA…).
- **Cedar + approval gating** for all writes: Cedar policies enforce read-only vs
  write-needs-approval per tool class at the Gateway (`cdk/policies/cedar/*.cedar`), and
  `shared/approval_guard.py` binds each write to an approval row with **payload-hash
  matching, an atomic compare-and-set "consume" (no replay), and a replay-window
  expiry** (verified). A generic write-capable MCP wired in directly would bypass all of this.
- **Cross-account hub-spoke** assume-role for target access (`shared/cluster_targets.py`),
  all DB access via **RDS Data API** (no direct driver/socket).
- **Audit**: agent SQL carries `/* source=dbops-agent */`.

Target DB access today is RDS-Data-API-only, so there is **no DocDB (Mongo) or
DynamoDB (SDK/PartiQL) data-plane path** in the agent/MCP layer — Foundation collects
only CloudWatch metrics + DescribeTable/DescribeDBClusters meta for those engines.

## Architectural mismatch with AWS-managed MCP servers

1. **Transport / hosting**: AWS DB MCP servers are self-run **local stdio** packages
   (`uvx`/docker), unlike the AWS Knowledge MCP which is an AWS-_hosted_ SigV4 endpoint.
   A read-only one need NOT go through the Gateway (the Knowledge pattern shows direct
   connection is fine) — the agent could run it as an in-Runtime stdio client. But because
   it is self-run (not a hosted endpoint), we still must host/run it ourselves (in the
   Runtime container or a Lambda/Fargate endpoint) with VPC reachability to the target DB
   and DB credentials. So it is feasible but not zero-cost integration.
2. **Auth / cross-account**: AWS MCP uses the local credential chain; ours uses
   hub-spoke assume-role + the Gateway IAM role. No native cross-account.
3. **Safety (decisive)**: our entire write-safety model is Cedar + approval-id/payload-hash
   gating. AWS MCP servers are write-capable by default, so wiring one in directly would
   let the agent mutate customer data **without** our human-in-the-loop approval. DynamoDB
   MCP has a read-only flag; not all awslabs servers do.
4. **Cache-first**: AWS MCP is live-query; it must not back dashboard rendering.

## Decision

**Do not replace our architecture with AWS-managed MCP servers, and do not wire them in
wholesale.** They are a generic "connect + run query" layer one level below our value
(cache + heuristics + Cedar approval + cross-account). For the deep-diagnosis specs
(#2/#3) that genuinely need to query the database itself, prefer **Option A: implement
the bounded set of read queries we need ourselves** (boto3 for DynamoDB, pymongo for
DocDB), inside our collectors/MCP, preserving cache-first, Cedar approval, cross-account,
and audit.

**Option B (adopt an AWS MCP read-only for the data plane)** is acceptable only where it
saves substantial connectivity work AND read-only is enforced **at the credential layer,
not just an MCP env flag**. The `DDB-MCP-READONLY`/tool-allowlist/prompt level is
necessary but NOT sufficient — defense-in-depth requires least-privilege enforcement at
every layer: (1) the MCP's read-only capability, (2) a **read-only DB user / least-privilege
scoped IAM role** so the credential physically cannot mutate, (3) network/secret scope, and
(4) a Gateway/agent tool allowlist. Note the Knowledge-MCP "read-only direct" precedent
does **not** automatically transfer: the Knowledge MCP has no customer-data mutation path,
whereas a data-plane DB MCP does — so the bar for Option B is higher.

**Cheapest middle path (preferred when in doubt):** use AWS MCP servers as **development /
reference fixtures only** (run them locally to learn which diagnostic queries/tools matter),
then **port the specific read queries into first-party bounded tools** under our cache/
Cedar/cross-account/audit model. This captures the reuse value with none of the runtime
attack surface.

### Per-engine guidance

- **Aurora (PG/MySQL)** — **No adoption.** AWS Aurora MCP is RDS-Data-API query execution,
  which we already have via `execute_sql`, minus our cache/heuristics/Cedar/cross-account/
  audit. Adopting it would be redundant and would weaken the safety model.
- **DynamoDB (#3)** — **Option A (unless AWS MCP is used only offline/reference).** The
  planned findings (throttle, capacity adequacy, GSI health, cost) derive from CloudWatch
  (already collected) + DescribeTable (already collected). Only hot-partition /
  key-distribution analysis needs data-plane reads, which are a small, bounded boto3
  addition that is simpler to govern than a write-capable MCP.
- **DocumentDB (#2)** — **The one place AWS MCP could genuinely save work — but the Option-B
  bar is high.** We have no Mongo connectivity; deep diagnosis (`serverStatus`, `currentOp`,
  slow-op) needs the Mongo wire protocol. At the start of spec #2 decide A (thin pymongo
  read collector in a VPC Lambda) vs B (AWS DocDB MCP read-only) — and B is allowed ONLY if
  read-only is enforced at multiple layers (a **read-only Mongo DB user**, scoped secret,
  VPC scope, tool allowlist), not just an MCP setting.
- **DynamoDB data-modeling MCP** — orthogonal design-time tool; useful as a _reference_ for
  #3 index/key recommendations, not a runtime dependency.
- **AWS API MCP (call-any-AWS-API)** — too broad/unsafe for our gated model; avoid.

## Consequences

- We keep one coherent safety/auth/cache model instead of a parallel ungated surface.
- We carry the (small) cost of implementing the specific diagnostic reads ourselves.
- Spec #3 needs no AWS-MCP integration. Spec #2 includes an explicit A-vs-B connectivity
  decision for Mongo-level diagnosis.
- AWS MCP servers remain a useful reference for which diagnostic queries/tools matter.

## Update 2026-06-12 — Mongo connectivity decision: Option A

The DocumentDB deep-diagnosis A-vs-B connectivity question (deferred above) is now
decided: **Option A — a thin, read-only pymongo collector running in-VPC**, NOT the
AWS DocDB MCP. Rationale:

- **Architectural fit.** Deep diagnosis is just another bounded-read collector that
  feeds the cache (metric_snapshots / cluster_health_findings); the dashboard + chat
  read the cache, consistent with "never call AWS in real-time for dashboard rendering."
  Option B would be a live, out-of-band call that bypasses the cache.
- **Value-layer preservation.** Option B sits below our cache/Cedar/audit layer (per
  this ADR's core finding). Option A keeps results inside it — chat reads the resulting
  findings through the existing Cedar-gated `get_maintenance_findings` tool.
- **The read-only guarantee is credential-level either way** — a least-privilege
  read-only Mongo user — so Option B saves no scoping work, only Mongo-client code,
  which mirrors our existing collectors.
- **Self-deploy portability.** A pymongo collector is self-contained CDK; an external
  managed MCP adds an auth/availability dependency that is fragile in the headless/cron
  context where collectors run.

**Enforcement (multi-layer read-only):** least-privilege read-only DocDB user, scoped
Secrets Manager secret (per cluster, like spoke_role_arn), in-VPC reachability only,
and a hardcoded allowlist of read-only commands (serverStatus, currentOp,
getProfilingStatus, system.profile reads) — never a generic eval/runCommand surface.

**Deployer setup required** (so the collector activates): provision a read-only Mongo
user on the DocDB cluster + a Secrets Manager secret with its credentials, and ensure
the ETL collector's VPC/SG can reach the cluster on 27017. Absent that secret the
collector no-ops gracefully (CloudWatch-based DocDB diagnosis continues unaffected).
Live verification on the kept demo cluster is therefore deferred until that setup exists.
