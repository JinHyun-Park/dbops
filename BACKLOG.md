# DBOps Backlog

**Status: empty. No open items.**

Every P1-P4 item and every defect from the 2026-08-02 live tool sweep (567 real
invocations across all five engine families) is shipped, deployed and verified.
The full record lives in git history; this file was cleared on 2026-08-03 rather
than deleted because shipped code, tests and CI comments point at it by name and
those pointers must keep resolving to something true.

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

WS access logging stays disabled because the `$connect` authorizer reads the
Cognito access token from the query string, so access logs would record it in
plaintext. The WS-ticket pattern (single-use ticket in the URL instead of the
token, enforced by an `ALL_OLD` + empty-old-image Deny) shipped 2026-07-30, so
this is now a mitigation that holds rather than a conditional one.

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
