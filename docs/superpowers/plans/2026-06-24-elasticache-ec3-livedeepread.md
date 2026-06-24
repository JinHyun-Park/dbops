# ElastiCache EC-3 Live Deep-Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only live Redis/Valkey/Memcached deep-read MCP tool (`INFO`/`SLOWLOG`/`CLIENT LIST`/`MEMORY STATS` and Memcached `stats`) that connects over the native protocol from the in-VPC operations MCP Lambda, with TLS + Secrets-Manager AUTH and cross-account secret read via assume-role.

**Architecture:** Mirror the DocDB native-protocol tool pattern (`set_docdb_profiler`): a monkeypatchable client factory + lazy client import + `lookup_cluster`/`session_for`; bundle `redis-py`/`pymemcache` via the existing `_PipLocalBundling`; register the read-only tool engine-gated on `live_read`.

**Tech Stack:** Python 3.12 (operations MCP Lambda, in `data.vpc`), `redis>=5`, `pymemcache>=4`, AWS CDK.

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **READ-ONLY:** the tool runs ONLY a fixed allowlist of inspector commands (Redis: `INFO`, `SLOWLOG GET`, `CLIENT LIST`, `MEMORY STATS`; Memcached: `stats`). No free-form command from the caller; NO `CONFIG SET`/`FLUSH*`/`SLOWLOG RESET`/`CLIENT KILL`. No approval gate (read-only), but engine-gated on `live_read`.
- **TLS + AUTH:** connect `ssl=True` when `tls_enabled`; AUTH token from a per-cluster Secrets Manager ARN (`auth_secret_arn`, operator-supplied at registration). Token is NEVER logged or echoed in error messages (host name is OK).
- **Cross-account = code path only:** read the secret + describe the cluster via `session_for(region, spoke_role_arn)` (assume spoke role). Network reachability (VPC peering/PrivateLink) is an operator prerequisite the tool does NOT create — same-account is the validated path.
- **Lazy client import:** `import redis` / `from pymemcache... import Client` INSIDE the factory so the module imports + unit-tests without the lib (mirrors DocDB's lazy `import pymongo`). Factories are module-level `_REDIS_FACTORY`/`_MEMCACHED_FACTORY` hooks tests can patch.
- **Tool never raises out:** all failures → `{"status": "error"|"unavailable", "reason": ...}`.

---

### Task 1: Registration `auth_secret_arn` field

**Files:**

- Modify: `api/clusters/handler.py` (`_register_elasticache` — accept + store `auth_secret_arn`)
- Test: extend `tests/unit/api/test_clusters_elasticache.py`

**Interfaces:**

- Produces: registry rows for ElastiCache carry `auth_secret_arn` (top-level + in `resource_details`), defaulting to `""`/absent when not provided.

- [ ] **Step 1: Write the failing test.** Add to `tests/unit/api/test_clusters_elasticache.py`:

```python
def test_register_stores_auth_secret_arn():
    fake = MagicMock()
    fake.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis", "Status": "available", "ClusterEnabled": False,
         "MemberClusters": ["my-redis-001"], "CacheNodeType": "cache.t4g.micro",
         "AuthTokenEnabled": True, "TransitEncryptionEnabled": True}]}
    table = _table()
    body = _body()
    body["auth_secret_arn"] = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:redis-auth"
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        r = handler._register_elasticache(table, body)
    assert r["statusCode"] in (201, 207)
    item = table.put_item.call_args.kwargs["Item"]
    assert item["auth_secret_arn"] == "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:redis-auth"
    assert item["resource_details"]["auth_secret_arn"].endswith("redis-auth")


def test_register_without_auth_secret_arn_defaults_empty():
    fake = MagicMock()
    fake.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "r", "Status": "available", "ClusterEnabled": False,
         "MemberClusters": ["r-001"], "CacheNodeType": "cache.t4g.micro"}]}
    table = _table()
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        handler._register_elasticache(table, _body(name="r"))
    item = table.put_item.call_args.kwargs["Item"]
    assert item.get("auth_secret_arn", "") == ""
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `python -m pytest tests/unit/api/test_clusters_elasticache.py -q` → FAIL (auth_secret_arn not stored).

- [ ] **Step 3: Modify `_register_elasticache`** in `api/clusters/handler.py`. Read the function first. Add near the top (after extracting account_id/region/name):

```python
    auth_secret_arn = body.get("auth_secret_arn", "")
```

In the `details = {...}` dicts (BOTH the replication-group and cache-cluster branches), add the key:

```python
                "auth_secret_arn": auth_secret_arn,
```

And in the `item = {...}` registry row, add a top-level field:

```python
        "auth_secret_arn": auth_secret_arn,
```

(Place it next to `spoke_role_arn`. Both branches' `details` get the key so it's present regardless of which describe path matched.)

- [ ] **Step 4: Run tests.**

Run: `python -m pytest tests/unit/api/test_clusters_elasticache.py -q` → PASS (existing + 2 new).
Run: `python -m pytest tests/unit -q` → no regression.

- [ ] **Step 5: Commit.**

```bash
git add api/clusters/handler.py tests/unit/api/test_clusters_elasticache.py
git commit -m "feat(elasticache): store auth_secret_arn at registration (EC-3 live-read credential)"
```

---

### Task 2: Live-read tool + bundle + handler registration

**Files:**

- Modify: `mcp-servers/requirements.txt` (add `redis>=5`, `pymemcache>=4`)
- Create: `mcp-servers/mcp_servers/operations/tools/elasticache_live_read.py`
- Modify: `mcp-servers/mcp_servers/operations/handler.py` (import + TOOLS entry + `_ENGINE_GATED_TOOLS`)
- Test: `tests/unit/mcp_servers/operations/test_elasticache_live_read.py` (create)

**Interfaces:**

- Consumes: `lookup_cluster(cluster_id)`, `session_for(region, role_arn)` (both from `mcp_servers.shared.cluster_targets`); `CAPABILITIES`/`_engine_family` (engine gate).
- Produces: `elasticache_live_read_impl(cache, cluster_id=None, sections=None, **_) -> dict`.

- [ ] **Step 1: Read the templates.** Read `mcp-servers/mcp_servers/operations/tools/set_docdb_profiler.py` (the `_client_factory` + module-level `_CLIENT_FACTORY` hook + `lookup_cluster` + lazy import pattern) and `mcp-servers/mcp_servers/operations/handler.py` (the `TOOLS` dict entry shape, the `_ENGINE_GATED_TOOLS` dict ~line 38, and how `_resolve_family` + the gate at ~478-493 work). Confirm `session_for` + `lookup_cluster` are exported from `mcp_servers/shared/cluster_targets.py`.

- [ ] **Step 2: Add the bundle deps.** Append to `mcp-servers/requirements.txt`:

```
redis>=5
pymemcache>=4
```

- [ ] **Step 3: Write the failing test.** Create `tests/unit/mcp_servers/operations/test_elasticache_live_read.py`:

```python
"""ElastiCache live deep-read tool — read-only, mocked connection."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_T = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/operations/tools/elasticache_live_read.py"
_spec = importlib.util.spec_from_file_location("ec_live_read", _T)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class _FakeRedis:
    """Records every method call so the test can assert the read-only allowlist."""
    def __init__(self):
        self.calls = []
    def info(self, section):
        self.calls.append(("info", section))
        return {"used_memory": 1024} if section == "memory" else {"section": section}
    def slowlog_get(self, n):
        self.calls.append(("slowlog_get", n))
        return [{"id": 1, "duration": 12000, "start_time": 1700000000, "command": ["GET", "bigkey", "x" * 500]}]
    def client_list(self):
        self.calls.append(("client_list",))
        return [{"addr": "10.0.0.1:1"}, {"addr": "10.0.0.2:2"}]
    def memory_stats(self):
        self.calls.append(("memory_stats",))
        return {"total.allocated": 2048, "keys.count": 10, "peak.allocated": 4096, "dataset.bytes": 900}


def _patch(monkeypatch_targets, redis_client=None, mc_stats=None, row=None, token="tok"):
    mod.lookup_cluster = lambda cid: row or {
        "region": "ap-northeast-2", "spoke_role_arn": "", "resource_name": "my-redis",
        "engine": "redis",
        "resource_details": {"engine": "redis", "tls_enabled": True, "auth_enabled": True,
                             "auth_secret_arn": "arn:secret"},
    }
    sess = MagicMock()
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis",
         "NodeGroups": [{"PrimaryEndpoint": {"Address": "my-redis.cache.amazonaws.com", "Port": 6379}}]}]}
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": token}
    sess.client.side_effect = lambda svc: ec if svc == "elasticache" else sm
    mod.session_for = lambda region, role_arn: sess
    if redis_client is not None:
        mod._REDIS_FACTORY = lambda host, port, password, tls: redis_client
    if mc_stats is not None:
        mc = MagicMock(); mc.stats.return_value = mc_stats
        mod._MEMCACHED_FACTORY = lambda host, port: mc


def test_redis_readonly_summary_and_allowlist():
    rc = _FakeRedis()
    _patch(None, redis_client=rc)
    r = mod.elasticache_live_read_impl(None, cluster_id="my-redis")
    assert r["status"] == "ok"
    assert r["info"]["memory"]["used_memory"] == 1024
    assert r["slowlog"][0]["duration_us"] == 12000
    assert len(r["slowlog"][0]["command"]) <= 130  # truncated
    assert r["clients"]["count"] == 2
    assert r["memory"]["total.allocated"] == 2048
    # READ-ONLY allowlist: only inspector methods were ever called
    called = {c[0] for c in rc.calls}
    assert called <= {"info", "slowlog_get", "client_list", "memory_stats"}


def test_memcached_path():
    row = {"region": "ap-northeast-2", "spoke_role_arn": "", "resource_name": "mc",
           "engine": "memcached",
           "resource_details": {"engine": "memcached", "tls_enabled": False, "auth_enabled": False}}
    _patch(None, mc_stats={b"curr_items": b"5", b"evictions": b"0", b"get_hits": b"100", b"get_misses": b"10"}, row=row)
    # cache-cluster endpoint resolution
    mod.session_for("x", "").client("elasticache").describe_replication_groups.side_effect = Exception("not rg")
    mod.session_for("x", "").client("elasticache").describe_cache_clusters.return_value = {"CacheClusters": [
        {"CacheClusterId": "mc", "ConfigurationEndpoint": {"Address": "mc.cache.amazonaws.com", "Port": 11211}}]}
    r = mod.elasticache_live_read_impl(None, cluster_id="mc")
    assert r["status"] == "ok" and r["engine"] == "memcached"
    assert r["memcached"]["curr_items"] == "5"


def test_missing_endpoint_unavailable():
    _patch(None, redis_client=_FakeRedis())
    ec = mod.session_for("x", "").client("elasticache")
    ec.describe_replication_groups.return_value = {"ReplicationGroups": []}
    ec.describe_cache_clusters.return_value = {"CacheClusters": []}
    r = mod.elasticache_live_read_impl(None, cluster_id="my-redis")
    assert r["status"] == "unavailable"


def test_connection_error_no_token_leak():
    class _Boom:
        def info(self, s): raise Exception("connection refused")
    _patch(None, redis_client=_Boom(), token="SUPERSECRET")
    r = mod.elasticache_live_read_impl(None, cluster_id="my-redis")
    assert r["status"] == "error"
    assert "SUPERSECRET" not in str(r)


def test_missing_cluster_id():
    assert mod.elasticache_live_read_impl(None)["status"] == "error"
```

- [ ] **Step 4: Run it to verify it fails.**

Run: `python -m pytest tests/unit/mcp_servers/operations/test_elasticache_live_read.py -q` → FAIL (module missing).

- [ ] **Step 5: Create `mcp-servers/mcp_servers/operations/tools/elasticache_live_read.py`:**

```python
"""ElastiCache live deep-read — read-only Redis/Valkey/Memcached inspector.

Connects over the native protocol from the in-VPC operations MCP Lambda and runs
a FIXED allowlist of read-only inspector commands (Redis: INFO, SLOWLOG GET,
CLIENT LIST, MEMORY STATS; Memcached: stats). No write/admin command and no
free-form command from the caller. TLS + Secrets-Manager AUTH; cross-account
secret + describe via assumed spoke role. Mirrors the DocDB native-protocol tool
pattern (lazy client import + monkeypatchable factory)."""

import json

from mcp_servers.shared.cluster_targets import lookup_cluster, session_for

_CONNECT_TIMEOUT = 5
_SLOWLOG_COUNT = 20
_CLIENT_SAMPLE = 10
_ARG_TRUNC = 120
_REDIS_INFO_SECTIONS = ["server", "clients", "memory", "stats", "replication", "keyspace"]


def _redis_factory(host, port, password, tls):
    import redis  # lazy: not importable in the test env
    return redis.Redis(
        host=host, port=int(port), password=(password or None),
        ssl=bool(tls), ssl_cert_reqs=None,
        socket_connect_timeout=_CONNECT_TIMEOUT, socket_timeout=_CONNECT_TIMEOUT,
        decode_responses=True,
    )


def _memcached_factory(host, port):
    from pymemcache.client.base import Client  # lazy
    return Client((host, int(port)), connect_timeout=_CONNECT_TIMEOUT, timeout=_CONNECT_TIMEOUT)


# Indirection hooks so tests can inject fakes without the libs installed.
_REDIS_FACTORY = _redis_factory
_MEMCACHED_FACTORY = _memcached_factory


def _resp(status, **kw):
    return {"status": status, **kw}


def _read_auth_token(secret_arn, sess):
    raw = (sess.client("secretsmanager").get_secret_value(SecretId=secret_arn).get("SecretString") or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            return d.get("auth_token") or d.get("password") or d.get("token")
        except Exception:
            return raw
    return raw


def _resolve_endpoint(ec_client, resource_name):
    try:
        rg = (ec_client.describe_replication_groups(ReplicationGroupId=resource_name)
              .get("ReplicationGroups") or [])
        if rg:
            g = rg[0]
            ce = g.get("ConfigurationEndpoint")
            if ce and ce.get("Address"):
                return ce["Address"], ce.get("Port", 6379)
            for ng in (g.get("NodeGroups") or []):
                pe = ng.get("PrimaryEndpoint")
                if pe and pe.get("Address"):
                    return pe["Address"], pe.get("Port", 6379)
    except Exception:
        pass
    try:
        cc = (ec_client.describe_cache_clusters(CacheClusterId=resource_name, ShowCacheNodeInfo=True)
              .get("CacheClusters") or [])
        if cc:
            c = cc[0]
            ce = c.get("ConfigurationEndpoint")
            if ce and ce.get("Address"):
                return ce["Address"], ce.get("Port", 11211)
            nodes = c.get("CacheNodes") or []
            if nodes and nodes[0].get("Endpoint"):
                ep = nodes[0]["Endpoint"]
                return ep.get("Address"), ep.get("Port", 6379)
    except Exception:
        pass
    return None, None


def _decode(d):
    out = {}
    for k, v in (d or {}).items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        out[k] = v
    return out


def elasticache_live_read_impl(cache, cluster_id=None, sections=None, **_):
    if not cluster_id:
        return _resp("error", reason="cluster_id required")
    row = lookup_cluster(cluster_id) or {}
    rd = row.get("resource_details") or {}
    if isinstance(rd, str):
        try:
            rd = json.loads(rd)
        except Exception:
            rd = {}
    engine = (rd.get("engine") or row.get("engine") or "redis").lower()
    region = row.get("region", "")
    role_arn = row.get("spoke_role_arn", "")
    tls = bool(rd.get("tls_enabled"))
    secret_arn = rd.get("auth_secret_arn") or row.get("auth_secret_arn")
    resource_name = row.get("resource_name") or cluster_id

    try:
        sess = session_for(region, role_arn)
    except Exception as e:
        return _resp("error", reason=f"session 생성 실패: {str(e)[:160]}", cluster_id=cluster_id)

    host, port = _resolve_endpoint(sess.client("elasticache"), resource_name)
    if not host:
        return _resp("unavailable", reason="도달 가능한 엔드포인트를 찾지 못했습니다", cluster_id=cluster_id)

    token = None
    if secret_arn:
        try:
            token = _read_auth_token(secret_arn, sess)
        except Exception as e:
            return _resp("error", reason=f"AUTH 시크릿 조회 실패: {str(e)[:160]}", cluster_id=cluster_id)

    is_memcached = engine == "memcached"
    try:
        if is_memcached:
            client = _MEMCACHED_FACTORY(host, port)
            stats = _decode(client.stats() or {})
            return _resp("ok", engine=engine, host=host, memcached=stats, cluster_id=cluster_id)
        client = _REDIS_FACTORY(host, port, token, tls)
        want = [s for s in (sections or _REDIS_INFO_SECTIONS) if s in _REDIS_INFO_SECTIONS]
        info = {}
        for sec in want:
            try:
                info[sec] = client.info(sec)
            except Exception:
                pass
        slow = []
        try:
            for e in (client.slowlog_get(_SLOWLOG_COUNT) or []):
                args = e.get("command")
                if isinstance(args, (list, tuple)):
                    args = " ".join(str(a) for a in args)
                slow.append({"id": e.get("id"), "duration_us": e.get("duration"),
                             "ts": e.get("start_time"), "command": str(args)[:_ARG_TRUNC]})
        except Exception:
            pass
        clients = {}
        try:
            cl = client.client_list() or []
            clients = {"count": len(cl), "sample": cl[:_CLIENT_SAMPLE]}
        except Exception:
            pass
        mem = {}
        try:
            ms = client.memory_stats() or {}
            mem = {k: ms.get(k) for k in ("total.allocated", "peak.allocated", "keys.count", "dataset.bytes") if k in ms}
        except Exception:
            pass
        return _resp("ok", engine=engine, host=host, info=info, slowlog=slow,
                     clients=clients, memory=mem, cluster_id=cluster_id)
    except Exception as e:
        return _resp("error", reason=f"연결/조회 실패: {str(e)[:160]}", host=host, cluster_id=cluster_id)
```

- [ ] **Step 6: Register the tool** in `mcp-servers/mcp_servers/operations/handler.py`:
  - Import (next to the other tool imports): `from mcp_servers.operations.tools.elasticache_live_read import elasticache_live_read_impl` (match the existing import style — confirm whether they use `from .tools.X import` or `from mcp_servers.operations.tools.X import`).
  - Add to `_ENGINE_GATED_TOOLS`: `"elasticache_live_read": "live_read",`.
  - Add to the `TOOLS` dict:

```python
    "elasticache_live_read": {
        "impl": elasticache_live_read_impl,
        "description": "ElastiCache only: live Redis/Valkey/Memcached deep-read — "
                       "INFO, SLOWLOG, CLIENT LIST, MEMORY STATS (Redis) or stats "
                       "(Memcached). Read-only; no mutation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Registered ElastiCache cluster id"},
                "sections": {"type": "array", "items": {"type": "string"},
                             "description": "Optional subset of Redis INFO sections "
                                            "(server/clients/memory/stats/replication/keyspace)"},
            },
            "required": ["cluster_id"],
        },
    },
```

- [ ] **Step 7: Write the engine-gate test.** Add to `tests/unit/mcp_servers/operations/` (mirror the existing docdb/ddb gate test — find `test_*` that exercises `_ENGINE_GATED_TOOLS` / the handler gate). Assert: `elasticache_live_read` against a cluster whose family is NOT elasticache (e.g. `_resolve_family` returns `relational`) → `unsupported_engine`; against an elasticache cluster (family `elasticache`, which has `live_read: True`) → reaches the impl (mock the impl or assert no gate refusal). If the existing gate tests are parametrized, add the new tool to the parametrization.

- [ ] **Step 8: Run tests.**

Run: `python -m pytest tests/unit/mcp_servers/operations/ -q` → PASS.
Run: `python -m pytest tests/unit -q` → no regression.

- [ ] **Step 9: Commit.**

```bash
git add mcp-servers/requirements.txt mcp-servers/mcp_servers/operations/tools/elasticache_live_read.py mcp-servers/mcp_servers/operations/handler.py tests/unit/mcp_servers/operations/
git commit -m "feat(elasticache): live deep-read MCP tool (INFO/SLOWLOG/CLIENT LIST/MEMORY STATS, read-only, TLS+AUTH)"
```

---

### Task 3: CDK — security group egress + IAM

**Files:**

- Modify: `cdk/stacks/agent_stack.py` (operations MCP Lambda: SG egress to cache ports + IAM for elasticache describe + assume-role; confirm secret get exists)

**Interfaces:**

- Consumes: the operations MCP Lambda construct + `data.vpc`. Produces: the Lambda can egress to 6379/11211 and has `elasticache:Describe*` + `sts:AssumeRole` + `secretsmanager:GetSecretValue`.

- [ ] **Step 1: Read the operations MCP Lambda block** in `cdk/stacks/agent_stack.py` (~lines 143-200): its current SG / `allow_all_outbound`, its `add_to_role_policy` statements (confirm `secretsmanager:GetSecretValue` on `*` at ~194; check for `sts:AssumeRole` and `elasticache:Describe*`). Read the `docdb_mongo_collector` SG block in `data_stack.py` (~244-249) as the SG precedent.

- [ ] **Step 2: Ensure egress to the cache ports.** If the operations Lambda has no explicit SG (uses the default data.vpc SG) and that SG already allows all outbound, no change is needed for egress — but make it explicit + documented: give the operations Lambda a dedicated SG with `allow_all_outbound=True` (mirror `DocDBMongoCollectorSG`), OR if a dedicated SG already exists, confirm `allow_all_outbound=True`. Add a one-line comment that this is for the ElastiCache (6379/11211) + DocDB (27017) native-protocol egress.

```python
        # (only if no suitable SG exists already)
        ops_mcp_sg = ec2.SecurityGroup(
            self, "OperationsMcpSG", vpc=data.vpc,
            description="operations MCP - egress to in-VPC DB native protocols (DocDB 27017, ElastiCache 6379/11211)",
            allow_all_outbound=True,
        )
        # attach ops_mcp_sg to the operations_mcp_lambda (security_groups=[ops_mcp_sg]) if not already VPC+SG configured
```

(If the Lambda is already VPC-attached with a working SG that allows outbound, do NOT churn it — just confirm + comment. The ElastiCache cluster's inbound SG is the operator's responsibility and is set up for the live-validation cluster separately.)

- [ ] **Step 3: Add IAM** to the operations MCP Lambda role (only the actions it lacks — read Step 1 findings):

```python
        operations_mcp_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticache:DescribeReplicationGroups",
                "elasticache:DescribeCacheClusters",
            ],
            resources=["*"],
        ))
        # sts:AssumeRole for cross-account secret/describe (add only if absent)
        operations_mcp_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=["arn:aws:iam::*:role/dbops-spoke-role"],
        ))
```

(`secretsmanager:GetSecretValue` on `*` already exists per the spec — do NOT duplicate. If `sts:AssumeRole` to the spoke role already exists on this role, skip that statement.)

- [ ] **Step 4: Run synth.**

Run: `python -m pytest tests/cdk/test_synth.py -q` → PASS.

- [ ] **Step 5: Commit.**

```bash
git add cdk/stacks/agent_stack.py
git commit -m "feat(elasticache): operations MCP egress + IAM for live deep-read (describe + assume-role)"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD` — focus: READ-ONLY (the tool runs only the fixed inspector allowlist; no write/admin command, no caller free-form command); token never logged/echoed; cross-account uses `session_for(spoke_role_arn)` for BOTH describe + secret; engine-gated on `live_read` (FAIL-CLOSED for non-ElastiCache); lazy lib import (module imports without redis); tool never raises out; IAM additions read-only + scoped.
- Deploy dev: `cdk deploy dbops-dev-agent` (operations MCP Lambda — code + bundle + SG/IAM). The bundle adds redis/pymemcache to the asset. No frontend change.
- **Live validation (authorized — user's account):**
  1. Create a temporary `cache.t4g.micro` Redis (cluster-mode disabled, 1 node, TransitEncryption + AUTH token) reachable by the operations Lambda (in `data.vpc` or a peered/same VPC), tag `dbops:temp-test=ec3`. Store the AUTH token in a Secrets Manager secret. Add an SG ingress rule on 6379 from the operations Lambda's SG.
  2. Register it (admin) with `auth_secret_arn`.
  3. Invoke `elasticache_live_read` (agent chat or direct MCP invoke) → confirm parsed INFO/SLOWLOG/CLIENT LIST/MEMORY STATS.
  4. Generate light load (redis-benchmark / SET-GET loop) → re-invoke → SLOWLOG/stats reflect it.
  5. **Tear down** cluster + secret + SG rule; confirm removal.
     (If VPC reachability is blocked in practice, fall back to the unit/mock coverage + document — same constraint EC-1/EC-2 noted.)
- Then `superpowers:finishing-a-development-branch`.
