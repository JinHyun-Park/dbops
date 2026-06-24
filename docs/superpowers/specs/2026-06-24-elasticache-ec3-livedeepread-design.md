# ElastiCache EC-3 — Live Redis/Memcached Deep-Read — Design

**Date:** 2026-06-24
**Status:** approved (user answered the EC-3 decisions: build now, cross-account support, Secrets Manager AUTH + TLS required, read-only; live validation against a real cluster in the user's account is authorized)

## Context

Third spec of the ElastiCache program. EC-1 (register + CloudWatch + dashboard)
and EC-2 (findings + RCA) give breadth from cached CloudWatch metrics. EC-3 adds
**depth** that CloudWatch cannot expose: a live connection to the cache over its
native protocol to pull `INFO`, `SLOWLOG GET`, `CLIENT LIST`, `MEMORY STATS`
(Redis/Valkey) and `stats`/`stats items`/`stats slabs` (Memcached) — surfacing
slow commands, big keys, per-section memory, connected clients, keyspace.

The direct precedent is the DocumentDB write tools (`set_docdb_profiler`,
`create_docdb_index`) which connect over the Mongo wire protocol via `pymongo`
from the in-VPC operations MCP Lambda, with a Secrets-Manager credential and a
bundled client lib (`cdk/bundling.py` `_PipLocalBundling`). EC-3 mirrors that
infrastructure for `redis-py` / `pymemcache`.

This spec covers **EC-3 only**. Non-goals: write/mutation (EC-4), simulation
(EC-5), new dashboard panels (the deep-read is an agent/MCP tool surfaced in chat;
a dashboard panel for it is a later nicety).

## Decisions (from the user)

- **Build EC-3 now**, unit + mock validated; live-validated against a **real
  ElastiCache cluster created in the user's AWS account** (a small
  `cache.t4g.micro` Redis with AUTH + TLS), including generating load — then torn
  down (tagged temporary test resource per the CDK-only-scope rule).
- **Cross-account support** at the code level: the tool reads the AUTH secret via
  an assumed spoke role (`session_for(region, spoke_role_arn)` — improving on the
  DocDB tools, which don't assume-role). **Network reachability across accounts
  (VPC peering / PrivateLink / Transit Gateway) is an operator-provisioned
  prerequisite** that EC-3 does NOT create — native TCP (6379/11211) to a
  cross-account VPC cannot be established by Lambda code alone. This limitation is
  documented; live validation is same-account (the only reachable path without
  peering).
- **Secrets Manager AUTH token + TLS required.** The tool connects with `ssl=True`
  (when `tls_enabled`) and an AUTH token read from a per-cluster Secrets Manager
  ARN supplied at registration. **Read-only commands only** — a hardcoded
  allowlist; any attempt to issue a write/admin command (`CONFIG SET`, `FLUSHALL`,
  `SLOWLOG RESET`, `CLIENT KILL`, …) is impossible because the tool never exposes
  arbitrary command execution — it runs a fixed set of inspector commands.

## Architecture

The live-read tool lives in the **operations MCP server** (already in `data.vpc`,
already wired for `_PipLocalBundling`, already the home of the DocDB
native-protocol tools). It is **read-only** (no approval gate) but engine-gated on
the `live_read` capability so it refuses non-ElastiCache clusters.

### Component 1 — Registration: AUTH secret ARN field

`api/clusters/handler.py` `_register_elasticache` accepts an optional
`auth_secret_arn` in the request body and stores it on the registry row (and in
`resource_details["auth_secret_arn"]`). The secret holds the Redis AUTH token —
either a raw string or `{"auth_token": "..."}` (the tool accepts both). Describe
APIs do not return the token, so the operator supplies the secret ARN at
registration, exactly like the DocDB `mongo_secret_arn` pattern. No schema
migration — it rides in the existing `resource_details` JSONB + a top-level
registry field. `auth_enabled=false` clusters may omit it (the tool connects
without a password).

### Component 2 — Client bundling

Add `redis>=5` and `pymemcache>=4` to `mcp-servers/requirements.txt`. The existing
`_PipLocalBundling("../mcp-servers")` on the operations MCP Lambda
(`agent_stack.py:157`) picks them up (manylinux2014 wheels). The other MCP servers
share the asset and get the libs too (harmless, unused) — same as pymongo today.

### Component 3 — Live-read tool `mcp-servers/mcp_servers/operations/tools/elasticache_live_read.py`

`elasticache_live_read_impl(cache, cluster_id, sections=None) -> dict`:

1. `lookup_cluster(cluster_id)` → registry row: `region`, `spoke_role_arn`,
   `resource_details` (engine, tls_enabled, auth_enabled, auth_secret_arn).
2. **Resolve endpoint** by describing the cluster via
   `session_for(region, spoke_role_arn).client("elasticache")`:
   replication group → `NodeGroups[].PrimaryEndpoint` (or `ConfigurationEndpoint`
   for cluster-mode); cache cluster → `CacheNodes[0].Endpoint` (Memcached uses the
   configuration endpoint). Yields `(host, port)`.
3. **Read the AUTH token** (if `auth_secret_arn` present) via
   `session_for(region, spoke_role_arn).client("secretsmanager").get_secret_value`
   — supports a raw-string secret or `{"auth_token": ...}`.
4. **Connect** with a monkeypatchable `_redis_factory(host, port, password, tls)`
   / `_memcached_factory(host, port)` (mirrors DocDB's `_client_factory`, so tests
   inject a fake): `redis.Redis(host, port, password=token, ssl=tls,
ssl_cert_reqs=None, socket_connect_timeout=5, socket_timeout=5)`.
5. **Run the read-only inspector commands** for the engine and return a parsed
   summary (NOT raw dumps):
   - **Redis/Valkey:** `INFO` (parsed sections: server, clients, memory,
     stats, replication, keyspace), `SLOWLOG GET 20` (top slow commands w/
     duration + truncated args), `CLIENT LIST` (count + a sample), `MEMORY STATS`
     (key fields). The `sections` arg optionally narrows which to fetch.
   - **Memcached:** `stats`, `stats items`, `stats slabs` via `pymemcache` (or a
     raw stats command). Returns hit ratio, evictions, items, memory.
6. **Read-only enforcement:** the tool issues ONLY the fixed inspector commands
   above. It never accepts a free-form command from the caller (`sections` only
   selects among the predefined INFO sections). No `CONFIG SET`/`FLUSH*`/
   `SLOWLOG RESET`/`CLIENT KILL`.
7. **Errors:** connection/timeout/auth failure → a structured `{"status":
"error", "reason": "..."}` (generic, no secret/host leakage beyond the host
   name) — never raises out; a missing endpoint/secret → `{"status":
"unavailable", "reason": "no reachable endpoint / no auth secret configured"}`.
   SLOWLOG truncates command args (avoid dumping key values / PII).

### Component 4 — Handler registration + engine gate

In `mcp-servers/mcp_servers/operations/handler.py`: import + add
`"elasticache_live_read"` to the `TOOLS` dict (description: "ElastiCache only:
live Redis/Memcached deep-read — INFO/SLOWLOG/CLIENT LIST/MEMORY STATS, read-only";
input_schema: `cluster_id` required, `sections` optional). Add it to
`_ENGINE_GATED_TOOLS` with capability `"live_read"` so a non-ElastiCache cluster
(or one whose family lacks `live_read`) is refused with `unsupported_engine` —
FAIL-CLOSED via the existing gate. No approval gate (read-only).

### Component 5 — CDK (SG + IAM)

`cdk/stacks/agent_stack.py` operations MCP Lambda:

- **Security group egress:** the Lambda is in `data.vpc`. Ensure it can egress to
  the cache ports. Mirror the `docdb_mongo_collector` approach: give the Lambda a
  dedicated SG with `allow_all_outbound=True` (or add explicit egress to
  6379/11211). The **ElastiCache cluster's SG must allow ingress from the Lambda's
  SG** — for the live-validation cluster we create, we add that ingress rule (and
  document it as the operator's responsibility for their own clusters).
- **IAM:** the operations Lambda already has `secretsmanager:GetSecretValue` on
  `*` (agent_stack.py:194) and `sts:AssumeRole` for cross-account (confirm; add if
  missing) + `elasticache:DescribeReplicationGroups`/`DescribeCacheClusters` (for
  endpoint resolution — add if the operations role lacks them). All read-only.

## Data Flow

Agent calls `elasticache_live_read(cluster_id)` → operations MCP Lambda (in-VPC) →
describe (resolve endpoint, assume spoke role if cross-account) → read AUTH secret
→ TCP+TLS connect to the cache → run read-only inspector commands → parsed summary
back to the agent. No data is cached/persisted (live, on-demand). Cross-account
requires a pre-existing network path (peering) — same-account is the validated
path.

## Error Handling

- Tool never raises out — all failures become `{"status": "error"/"unavailable",
"reason": ...}` with no secret/token leakage.
- Connect/socket timeouts are short (5s) to fail fast (mirrors DocDB's
  `serverSelectionTimeoutMS`).
- Engine gate refuses non-ElastiCache (FAIL-CLOSED).
- `redis`/`pymemcache` import is lazy (inside the factory) so the module imports
  in the test environment without the lib (mirrors DocDB's lazy `import pymongo`).

## Testing

- **Tool unit** (`tests/unit/mcp_servers/operations/test_elasticache_live_read.py`),
  monkeypatching `_redis_factory`/`_memcached_factory` + `lookup_cluster` +
  `session_for`: Redis path parses INFO/SLOWLOG/CLIENT LIST/MEMORY STATS into the
  summary; Memcached path uses the stats commands; missing endpoint/secret →
  `unavailable`; connection error → `error` (no token in the message); SLOWLOG
  args truncated; `sections` narrows INFO. **Read-only**: assert the fake client
  only ever received the allowlisted commands.
- **Engine-gate unit**: `elasticache_live_read` on a non-ElastiCache cluster →
  `unsupported_engine` (via `_ENGINE_GATED_TOOLS` + `live_read`); on an
  ElastiCache cluster → reaches the impl. (Mirror the existing docdb/ddb gate
  tests.)
- **CDK**: `tests/cdk/test_synth.py` green (SG/IAM additions synth cleanly).
- Full unit suite green. AST-parse the agent/ nothing (no agent/ change).

## Live validation (post-merge, in the user's account — authorized)

1. Create a temporary `cache.t4g.micro` **Redis (cluster-mode disabled, 1 node)**
   with **TransitEncryption + an AUTH token** in `data.vpc` (or a VPC reachable
   by the operations Lambda), tagged `dbops:temp-test=ec3`. Put the AUTH token in
   a Secrets Manager secret. Add an SG ingress rule allowing the operations
   Lambda's SG on 6379.
2. Register it (admin) with `auth_secret_arn` = the secret.
3. Invoke `elasticache_live_read` (via the agent / a direct MCP invoke) → confirm
   it returns parsed INFO/SLOWLOG/CLIENT LIST/MEMORY STATS.
4. Generate light load (e.g. `redis-benchmark` or a SET/GET loop) → re-invoke →
   confirm SLOWLOG / stats reflect the activity.
5. **Tear down** the cluster + secret + SG rule; confirm removal.
   (If same-account VPC reachability proves blocked, fall back to unit/mock
   coverage + document — same constraint EC-1/EC-2 noted.)

## Security

- **Read-only:** a fixed inspector-command allowlist; no arbitrary command
  execution, no write/admin commands. No mutation of the cache.
- **TLS required** when `tls_enabled`; AUTH token via Secrets Manager (never
  logged; not echoed in error messages).
- **Cross-account** secret read via assumed spoke role (least-privilege: the
  operations role assumes `spoke_role_arn`; the spoke role grants read of its own
  secret + describe). Network reachability is the operator's peering
  responsibility — flagged, not silently assumed.
- IAM additions are all read-only (describe + secret get + assume-role).
