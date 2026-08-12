# DBOps Backlog

**Status: three open items, none a defect. Everything else is closed.**

Every P1-P4 item and every defect from the 2026-08-02 live tool sweep (567 real
invocations across all five engine families) is shipped, deployed and verified.
The full record lives in git history; this file was cleared on 2026-08-03 rather
than deleted because shipped code, tests and CI comments point at it by name and
those pointers must keep resolving to something true.

## Open

### Triage the 3 CodeQL alerts that arrived with the public flip

CodeQL default setup was enabled 2026-08-11 (it cannot run on a private free-tier
repo, so this is the first time this scanner has ever seen the code). It opened 3
alerts. Alerts are visible only to collaborators, so nothing is publicly exposed.

My preliminary read is that all three are false positives, but a security alert should
be dismissed with a recorded reason by someone who looked, not by whoever happened to
enable the scanner:

| rule                                              | location                                                                   | preliminary read                                                                                                                                                                                                                      |
| ------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `js/insecure-randomness` (high)                   | `frontend/src/lib/agentcore-sse.ts:142` (sink; sources elsewhere)          | The 3 `Math.random()` uses in the frontend are local list keys: `SavedPlan.id`, `SavedView.id`, toast id. The real session id uses `crypto.randomUUID()` at `agentcore-sse.ts:49` and is length-checked. No traced path reaches auth. |
| `py/clear-text-logging-sensitive-data` (high)     | `mcp-servers/mcp_servers/operations/tools/modify_dynamodb_capacity.py:270` | The `logger.warning` logs `cluster_id`, `table`, `target_mode`, `eff_rcu`, `eff_wcu` plus `exc_info`. None is a credential. Worth confirming what CodeQL traced as the sensitive source before dismissing.                            |
| `py/incomplete-url-substring-sanitization` (high) | `tests/unit/api/test_incident_webhook.py:58`                               | Test-only assertion, not shipped code.                                                                                                                                                                                                |

GitHub's own secret scanning found **0** on the same tree, which is independent
confirmation of the gitleaks result.

```bash
gh api 'repos/JinHyun-Park/dbops/code-scanning/alerts?state=open' \
  --jq '.[] | .number, .rule.id, .most_recent_instance.location.path'
```

### `npm run lint` reports 113 problems (97 errors, 16 warnings)

Surfaced 2026-08-04. These are not new code: `eslint` was pinned to `^10` while
`eslint-config-next` bundles an `eslint-plugin-react` that supports at most ESLint
9.7, so lint CRASHED instead of reporting, and nothing gated it (CI runs build plus
typecheck; pre-commit runs prettier, ruff, tsc, gitleaks). Pinning `eslint ^9` made
it runnable and the backlog became visible all at once.

Dominant finding by far: `Calling setState synchronously within an effect can
trigger cascading renders`, a rule that arrived with Next 16. Worth triaging rather
than bulk-suppressing, because this codebase has already shipped one real bug of
exactly that family (a mount-once effect whose async `.then` closed over empty
state and clobbered a later value). Some of the 97 are probably that bug again;
most are probably benign. Reading them is the work.

Do NOT add lint to CI before they are addressed: a gate that is red on its first run
teaches people to ignore gates.

```bash
cd frontend && npx eslint .
```

### The agent stack is at 498 of 500 CloudFormation resources

Measured 2026-08-12 from `cdk.out/dbops-dev-agent.template.json`. 500 is a hard
CloudFormation limit, so the headroom is **2 resources**.

This entry said 478 (measured 2026-08-04) until the APM feature landed. That one
feature, 8 REST routes and a Lambda and a DynamoDB table, consumed 20 of the 22
remaining slots. The estimate below was right and the margin is now effectively
gone: the next new route or MCP tool is expected to cross the line.

What makes this worth recording is the failure mode: the stack grows by roughly
3 to 6 resources per new REST route and per new MCP tool, so a normal-sized feature
can cross the line, and the error surfaces at deploy time as a limit rejection
rather than as anything pointing at the change that caused it.

The fix is a structural decision, not a patch: split the stack (nested stacks for
the REST API surface, or move the four MCP Lambdas out). That is an owner's call
about deployment topology, so it is recorded rather than pre-empted. Anyone about to
add a route or a tool should check the count first:

```bash
python3 -c "import json;print(len(json.load(open('cdk/cdk.out/dbops-dev-agent.template.json'))['Resources']))"
```

## Decisions that are NOT open items

Recorded here so they stop reading like unfinished work, and because the code
comments below cite this file.

### cdk-nag suppressions (`cdk/app.py`)

`cdk-nag` (AwsSolutions ruleset) runs in the `cdk-synth` CI job behind
`CDK_NAG=1` and the synth exit code is the gate, so a new unsuppressed finding
cannot merge. Stack-level suppressions cover choices that are deliberate for a
single-tenant, self-hosted DBA tool; the specific findings (S3 SSL, DDB PITR,
Cognito MFA, RDS, CloudFront, APIG, VPC flow logs, Secrets rotation) are triaged
individually, each as a fix or a per-resource suppression carrying its reason.
Every suppression is an argument made in public. Cognito MFA is the one open
product decision, deliberately out of scope: it is a policy call for whoever
deploys this, not a code gap.

WS access logging stays disabled because nothing consumes it, NOT because enabling
it would leak a credential. The `$connect` identity source is
`route.request.querystring.ticket`, a random 60-second single-use ticket (enforced by
an `ALL_OLD` + empty-old-image Deny) that `$connect` spends before any log line is
written, so a recorded ticket is worth nothing.

The first version of this paragraph said the Cognito access token was in the query
string. That described the pre-2026-07-30 design, and the same stale claim was sitting
in two `cdk/app.py` suppression reasons, which is worse: a suppression is an argument
made in public. Both are corrected. Enabling WS access logging is now safe and
unblocked; it just needs a retention decision.

### ElastiCache Redis multi-SKU price ambiguity (unfixed, bounded)

`elasticache_pricing.py` selects the Price List SKU with no `ExtendedSupport`
usagetype segment, because AWS returns several SKUs per node type that are
different CHARGES, not competing prices: `APN2-NodeUsage:` is the node
($0.024 for cache.t4g.micro) while `ExtendedSupportYr1_Yr2` ($0.019) and `Yr3`
are EOL surcharges. Picking by API ordering mixed them. Valkey has exactly one
SKU today only because it is new enough to carry no surcharges. Where every SKU
for a node type is a surcharge the lookup reports a MISS rather than a
surcharge, and soft-fails to `None` so a region with no matching SKU degrades to
the existing fallback instead of to a wrong number.

Residual: for a genuine Redis cluster whose region has several non-surcharge
node SKUs, the selection is still first-match. Bounded and reported, not silent.

### `ADD PRIMARY KEY` is not an index build (`ddl_estimator.py`)

`_INDEX_BUILD_RX` deliberately excludes `ADD PRIMARY KEY`: it rebuilds the table
rather than adding a secondary index, so it belongs to the rewrite cost class
(`_REBUILD_OPERATIONS`), which holds a second copy of the table until commit.
Putting it in both lists is how it previously ended up in neither.

### `ManagedBy=dbops` tag preflight is a WARNING, not a gate (complete)

The spoke template gates 15 write actions on `aws:ResourceTag/ManagedBy=dbops`,
and an untagged spoke-account resource is denied AFTER the approval is consumed.
`shared/managed_tag_preflight.py` reads the gated resource's own tags and puts a
`warning` on the `approval_required` card. All 15 actions are wired, across 18
call sites; `tests/unit/mcp_servers/shared/test_managed_tag_preflight_resolvers.py`
reads the spoke template and asserts every gated action is named at a preflight
site, so the coverage count cannot drift into prose again.

It stays a warning rather than a pre-consume refusal: refusing would assert what
the SPOKE role's policy says, and this code does not read that policy. Customers
adapt that template, so an untagged resource is not reliably a denial, and a
refusal would block parameter tuning outright for a deployment that dropped the
condition. The IAM condition remains the enforcement point.

`SnapshotCreate` correctly has no preflight: an `aws:ResourceTag` condition on a
Create action denies unconditionally, since the resource does not exist yet.

### `AllowedValues` is not parsed

The RDS field is free-form ("0-4294967295", "ON,OFF", enumerations with ranges
mixed in) and a parser that misread it would refuse writes that are legal, which
is worse than one wasted approval on a value the DBA can see in the response.
Upgrade path if it ever becomes worth it: validate ONLY the two unambiguous
shapes (a single `lo-hi` integer range and a pure comma-separated enumeration)
and stay silent on everything else.

### `schema_snapshots` is PostgreSQL-only by decision

Not a gap. On MySQL `information_schema` is privilege-filtered, so a REVOKE is
byte-identical to a DROP in every diff bucket. All five readers report
`unsupported_engine` rather than an empty success. Contract:
`shared/schema_diff_util.py`. Do not reconnect a MySQL read without a scope key
that provably moves when visibility moves.

### Broadcast efficiency (premature at current scale)

`broadcast()` posts sequentially and `alert_evaluator` re-scans the connections
table per fired rule. This only matters at hundreds of concurrent connections;
current deployments are UI-driven and small. When it matters: scan connections
once per evaluator run and reuse, and set explicit short `connect_timeout` /
`read_timeout` on the management client.
