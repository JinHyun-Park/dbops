# ElastiCache EC-4 Write Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four approval-gated ElastiCache write tools (node-type scaling, snapshot, reboot, failover-test) in the operations MCP server, mirroring the DynamoDB/DocDB write model.

**Architecture:** Each tool mirrors `modify_dynamodb_capacity`: REQUEST `describe` → `approval_required` → `verify_approval` (consume payload-bound row) → EXECUTE the control-plane mutation via `client_for_cluster(cluster_id, "elasticache")` (cross-account assume-role). Engine-gated on `elasticache_write` (FAIL-CLOSED). Payload projections in `approval_guard`. Parity entries in `cdk/tool_definitions.py`. IAM for the four write actions.

**Tech Stack:** Python 3.12 (operations MCP Lambda), boto3 `elasticache` control-plane, AWS CDK.

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **Every mutation is approval-gated, FAIL-CLOSED:** no `approved=True` → `approval_required`; `verify_approval` must return `ok` (consuming the payload-bound, single-use row) before any AWS write. A guard failure → `approval_denied`, NO mutation.
- **Engine-gated `elasticache_write`:** all four tools in `_ENGINE_GATED_TOOLS`; a non-ElastiCache / unresolvable cluster → `unsupported_engine` (the gate reads cluster_meta via `_resolve_family`).
- **Cross-account via `client_for_cluster(cluster_id, "elasticache")`** (assume-role; control-plane API needs no VPC path).
- **Op applicability:** snapshot + failover are Redis/Valkey only (Memcached → `unsupported_engine`); failover needs a replica (else `invalid`).
- **Never raises out:** all paths return a status dict; `ClientError` → `{"status":"error", reason: str(e)[:200]}`.
- **Korean** user-facing `reason`/warnings; AWS tokens (node types, names) verbatim.
- **`cdk/tool_definitions.py` parity** entry required for EVERY new TOOLS entry (parity test).
- The ElastiCache name = `lookup_cluster(cluster_id)["resource_name"]`; the elasticache client = `client_for_cluster(cluster_id, "elasticache")`. Engine = `lookup_cluster(cluster_id)["resource_details"]["engine"]` (default "redis").

---

### Task 1: `modify_elasticache_node_type` + `create_elasticache_snapshot`

**Files:**

- Create: `mcp-servers/mcp_servers/operations/tools/modify_elasticache_node_type.py`
- Create: `mcp-servers/mcp_servers/operations/tools/create_elasticache_snapshot.py`
- Modify: `mcp-servers/mcp_servers/shared/approval_guard.py` (2 projections)
- Modify: `mcp-servers/mcp_servers/operations/handler.py` (import + 2 TOOLS + 2 gate entries + `_CAP_LABEL["elasticache_write"]`)
- Modify: `cdk/tool_definitions.py` (2 parity entries)
- Test: `tests/unit/mcp_servers/operations/test_elasticache_writes.py` (create)

**Interfaces:**

- Consumes: `verify_approval(approval_id, cluster_id, action_type, payload=)`, `client_for_cluster(cluster_id, service)`, `lookup_cluster(cluster_id)` (from shared).
- Produces: `modify_elasticache_node_type_impl`, `create_elasticache_snapshot_impl`.

- [ ] **Step 1: Read the templates.** Read `mcp-servers/mcp_servers/operations/tools/modify_dynamodb_capacity.py` (the full REQUEST→approval_required→verify_approval→EXECUTE flow), `mcp-servers/mcp_servers/shared/approval_guard.py` `_project_payload` (~line 89, the per-action branches; note the existing `create_snapshot`/`modify_scaling` relational projections — ours are NEW distinct action_types) + `verify_approval` signature (~245), and `mcp-servers/mcp_servers/operations/handler.py` (TOOLS entry shape, `_ENGINE_GATED_TOOLS` ~38, `_CAP_LABEL` ~51, import style). Confirm `client_for_cluster` + `lookup_cluster` in `mcp_servers/shared/cluster_targets.py`.

- [ ] **Step 2: Write the failing test.** Create `tests/unit/mcp_servers/operations/test_elasticache_writes.py`:

```python
"""ElastiCache write tools — approval-gated, FAIL-CLOSED."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_BASE = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/operations/tools"


def _load(name):
    p = _BASE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

nodetype = _load("modify_elasticache_node_type")
snapshot = _load("create_elasticache_snapshot")


def _wire(mod, engine="redis", node_type="cache.t4g.micro", guard_ok=True):
    """Patch lookup_cluster + client_for_cluster + verify_approval on the module."""
    mod.lookup_cluster = lambda cid: {
        "resource_name": "my-redis", "region": "ap-northeast-2", "spoke_role_arn": "",
        "resource_details": {"engine": engine},
    }
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis", "CacheNodeType": node_type,
         "MemberClusters": ["my-redis-001"],
         "NodeGroups": [{"NodeGroupId": "0001", "NodeGroupMembers": [{"CacheClusterId": "my-redis-001"}]}]}]}
    mod.client_for_cluster = lambda cid, svc: ec
    mod.verify_approval = lambda *a, **k: {"ok": True} if guard_ok else {"ok": False, "reason": "denied"}
    return ec


def test_node_type_requires_approval():
    ec = _wire(nodetype)
    r = nodetype.modify_elasticache_node_type_impl(None, cluster_id="my-redis", node_type="cache.r7g.large")
    assert r["status"] == "approval_required"
    ec.modify_replication_group.assert_not_called()


def test_node_type_executes_when_approved():
    ec = _wire(nodetype)
    r = nodetype.modify_elasticache_node_type_impl(
        None, cluster_id="my-redis", node_type="cache.r7g.large", approved=True, approval_id="a1")
    assert r["status"] == "ok"
    ec.modify_replication_group.assert_called_once()
    kw = ec.modify_replication_group.call_args.kwargs
    assert kw["CacheNodeType"] == "cache.r7g.large" and kw["ApplyImmediately"] is True


def test_node_type_guard_denied_no_write():
    ec = _wire(nodetype, guard_ok=False)
    r = nodetype.modify_elasticache_node_type_impl(
        None, cluster_id="my-redis", node_type="cache.r7g.large", approved=True, approval_id="a1")
    assert r["status"] == "approval_denied"
    ec.modify_replication_group.assert_not_called()


def test_node_type_equal_rejected():
    ec = _wire(nodetype, node_type="cache.r7g.large")
    r = nodetype.modify_elasticache_node_type_impl(None, cluster_id="my-redis", node_type="cache.r7g.large")
    assert r["status"] in ("invalid", "error")
    ec.modify_replication_group.assert_not_called()


def test_snapshot_requires_approval():
    ec = _wire(snapshot)
    r = snapshot.create_elasticache_snapshot_impl(None, cluster_id="my-redis", snapshot_name="snap1")
    assert r["status"] == "approval_required"
    ec.create_snapshot.assert_not_called()


def test_snapshot_executes_when_approved():
    ec = _wire(snapshot)
    r = snapshot.create_elasticache_snapshot_impl(
        None, cluster_id="my-redis", snapshot_name="snap1", approved=True, approval_id="a1")
    assert r["status"] == "ok"
    ec.create_snapshot.assert_called_once()
    assert ec.create_snapshot.call_args.kwargs["SnapshotName"] == "snap1"


def test_snapshot_memcached_unsupported():
    ec = _wire(snapshot, engine="memcached")
    r = snapshot.create_elasticache_snapshot_impl(None, cluster_id="mc", snapshot_name="s")
    assert r["status"] == "unsupported_engine"
    ec.create_snapshot.assert_not_called()
```

- [ ] **Step 3: Run it to verify it fails.**

Run: `python -m pytest tests/unit/mcp_servers/operations/test_elasticache_writes.py -q` → FAIL (modules missing).

- [ ] **Step 4: Create `modify_elasticache_node_type.py`:**

```python
"""modify_elasticache_node_type — approval-gated ElastiCache node-type scaling
(modify_replication_group CacheNodeType). Mirrors the operations write model:
REQUEST describe → approval_required → verify_approval (consume) → EXECUTE.
Cross-account via client_for_cluster (control-plane API). Never raises out."""

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster


def _rg(client, name):
    rg = (client.describe_replication_groups(ReplicationGroupId=name).get("ReplicationGroups") or [])
    return rg[0] if rg else None


def modify_elasticache_node_type_impl(cache, cluster_id=None, node_type=None,
                                      approved=False, approval_id=None, **_):
    if not cluster_id or not node_type:
        return {"status": "invalid", "reason": "cluster_id와 node_type가 필요합니다", "cluster_id": cluster_id}
    row = lookup_cluster(cluster_id) or {}
    name = row.get("resource_name") or cluster_id
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        g = _rg(client, name)
    except Exception as e:
        return {"status": "error", "reason": f"조회 실패: {str(e)[:200]}", "cluster_id": cluster_id}
    if not g:
        return {"status": "error", "reason": "replication group을 찾지 못했습니다", "cluster_id": cluster_id}
    current = g.get("CacheNodeType", "")
    if current == node_type:
        return {"status": "invalid", "reason": f"이미 {node_type} 입니다", "cluster_id": cluster_id}

    payload = {"target": cluster_id, "node_type": node_type}
    if not approved:
        return {
            "status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
            "node_type": node_type, "current_state": {"node_type": current},
            "warnings": [f"노드 타입 변경은 적용 중 일시적 영향이 있을 수 있습니다. 현재={current} → 변경={node_type}"],
        }
    guard = verify_approval(approval_id, cluster_id, "modify_elasticache_node_type", payload=payload)
    if not guard.get("ok"):
        return {"status": "approval_denied", "reason": guard.get("reason", "approval guard rejected"), "cluster_id": cluster_id}
    try:
        client.modify_replication_group(ReplicationGroupId=name, CacheNodeType=node_type, ApplyImmediately=True)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        return {"status": "error", "reason": f"modify_replication_group 실패: {code or str(e)[:200]}", "cluster_id": cluster_id}
    return {"status": "ok", "cluster_id": cluster_id, "node_type": node_type}
```

- [ ] **Step 5: Create `create_elasticache_snapshot.py`:**

```python
"""create_elasticache_snapshot — approval-gated ElastiCache (Redis/Valkey) backup
(create_snapshot). Memcached has no snapshots → unsupported_engine. Mirrors the
operations write model. Never raises out."""

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster


def create_elasticache_snapshot_impl(cache, cluster_id=None, snapshot_name=None,
                                     approved=False, approval_id=None, **_):
    if not cluster_id or not snapshot_name:
        return {"status": "invalid", "reason": "cluster_id와 snapshot_name이 필요합니다", "cluster_id": cluster_id}
    row = lookup_cluster(cluster_id) or {}
    rd = row.get("resource_details") or {}
    engine = (rd.get("engine") or row.get("engine") or "redis").lower()
    if engine == "memcached":
        return {"status": "unsupported_engine", "reason": "Memcached는 스냅샷을 지원하지 않습니다", "cluster_id": cluster_id}
    name = row.get("resource_name") or cluster_id

    payload = {"target": cluster_id, "snapshot_name": snapshot_name}
    if not approved:
        return {
            "status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
            "snapshot_name": snapshot_name,
            "warnings": ["스냅샷 생성은 메모리/성능에 일시적 영향이 있을 수 있습니다(특히 단일 노드)."],
        }
    guard = verify_approval(approval_id, cluster_id, "create_elasticache_snapshot", payload=payload)
    if not guard.get("ok"):
        return {"status": "approval_denied", "reason": guard.get("reason", "approval guard rejected"), "cluster_id": cluster_id}
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        client.create_snapshot(ReplicationGroupId=name, SnapshotName=snapshot_name)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        return {"status": "error", "reason": f"create_snapshot 실패: {code or str(e)[:200]}", "cluster_id": cluster_id}
    return {"status": "ok", "cluster_id": cluster_id, "snapshot_name": snapshot_name}
```

- [ ] **Step 6: Add the 2 approval projections** to `_project_payload` in `mcp-servers/mcp_servers/shared/approval_guard.py` (after the existing branches, before the fallback):

```python
    if action_type == "modify_elasticache_node_type":
        return {"target": payload.get("target"), "node_type": payload.get("node_type")}
    if action_type == "create_elasticache_snapshot":
        return {"target": payload.get("target"), "snapshot_name": payload.get("snapshot_name")}
```

- [ ] **Step 7: Register the 2 tools** in `mcp-servers/mcp_servers/operations/handler.py`:
  - Imports (match existing style): `from mcp_servers.operations.tools.modify_elasticache_node_type import modify_elasticache_node_type_impl` and the snapshot one.
  - `_ENGINE_GATED_TOOLS`: add `"modify_elasticache_node_type": "elasticache_write"`, `"create_elasticache_snapshot": "elasticache_write"`.
  - `_CAP_LABEL`: add `"elasticache_write": "ElastiCache 클러스터"`.
  - `TOOLS`: 2 entries, e.g.:

```python
    "modify_elasticache_node_type": {
        "impl": modify_elasticache_node_type_impl,
        "description": "ElastiCache only: scale the node type (modify_replication_group). Approval-gated write.",
        "input_schema": {"type": "object", "properties": {
            "cluster_id": {"type": "string"}, "node_type": {"type": "string"},
            "approved": {"type": "boolean"}, "approval_id": {"type": "string"}},
            "required": ["cluster_id", "node_type"]},
    },
    "create_elasticache_snapshot": {
        "impl": create_elasticache_snapshot_impl,
        "description": "ElastiCache (Redis/Valkey) only: create a backup snapshot. Approval-gated write.",
        "input_schema": {"type": "object", "properties": {
            "cluster_id": {"type": "string"}, "snapshot_name": {"type": "string"},
            "approved": {"type": "boolean"}, "approval_id": {"type": "string"}},
            "required": ["cluster_id", "snapshot_name"]},
    },
```

- [ ] **Step 8: Add 2 parity entries** to `cdk/tool_definitions.py` (mirror the format of the EC-3 `elasticache_live_read` entry / the dynamodb write entries — read the file to match `_tool(...)` shape).

- [ ] **Step 9: Run tests.**

Run: `python -m pytest tests/unit/mcp_servers/operations/test_elasticache_writes.py -q` → PASS.
Run: `python -m pytest tests/unit -q` → no regression (the tool_definitions parity test passes).

- [ ] **Step 10: Commit.**

```bash
git add mcp-servers/mcp_servers/operations/tools/modify_elasticache_node_type.py mcp-servers/mcp_servers/operations/tools/create_elasticache_snapshot.py mcp-servers/mcp_servers/shared/approval_guard.py mcp-servers/mcp_servers/operations/handler.py cdk/tool_definitions.py tests/unit/mcp_servers/operations/test_elasticache_writes.py
git commit -m "feat(elasticache): approval-gated node-type scaling + snapshot write tools"
```

---

### Task 2: `reboot_elasticache` + `test_elasticache_failover`

**Files:**

- Create: `mcp-servers/mcp_servers/operations/tools/reboot_elasticache.py`
- Create: `mcp-servers/mcp_servers/operations/tools/test_elasticache_failover.py`
- Modify: `mcp-servers/mcp_servers/shared/approval_guard.py` (2 more projections)
- Modify: `mcp-servers/mcp_servers/operations/handler.py` (import + 2 TOOLS + 2 gate entries)
- Modify: `cdk/tool_definitions.py` (2 parity entries)
- Test: extend `tests/unit/mcp_servers/operations/test_elasticache_writes.py` + add engine-gate cases

**Interfaces:**

- Produces: `reboot_elasticache_impl`, `test_elasticache_failover_impl`.

- [ ] **Step 1: Write the failing tests.** Append to `test_elasticache_writes.py`:

```python
reboot = _load("reboot_elasticache")
failover = _load("test_elasticache_failover")


def _wire_reboot(mod, members=("my-redis-001",), guard_ok=True):
    mod.lookup_cluster = lambda cid: {"resource_name": "my-redis", "resource_details": {"engine": "redis"}}
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis",
         "NodeGroups": [{"NodeGroupId": "0001",
                         "NodeGroupMembers": [{"CacheClusterId": m} for m in members]}],
         "MemberClusters": list(members)}]}
    ec.describe_cache_clusters.return_value = {"CacheClusters": [
        {"CacheClusterId": members[0], "CacheNodes": [{"CacheNodeId": "0001"}]}]}
    mod.client_for_cluster = lambda cid, svc: ec
    mod.verify_approval = lambda *a, **k: {"ok": True} if guard_ok else {"ok": False, "reason": "denied"}
    return ec


def test_reboot_requires_approval_then_executes():
    ec = _wire_reboot(reboot)
    r1 = reboot.reboot_elasticache_impl(None, cluster_id="my-redis")
    assert r1["status"] == "approval_required"
    ec.reboot_cache_cluster.assert_not_called()
    r2 = reboot.reboot_elasticache_impl(None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r2["status"] == "ok"
    ec.reboot_cache_cluster.assert_called_once()


def test_reboot_guard_denied_no_write():
    ec = _wire_reboot(reboot, guard_ok=False)
    r = reboot.reboot_elasticache_impl(None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r["status"] == "approval_denied"
    ec.reboot_cache_cluster.assert_not_called()


def _wire_failover(mod, replicas=1, guard_ok=True):
    mod.lookup_cluster = lambda cid: {"resource_name": "my-redis", "resource_details": {"engine": "redis"}}
    ec = MagicMock()
    members = [{"CacheClusterId": "my-redis-001"}]
    if replicas:
        members.append({"CacheClusterId": "my-redis-002"})
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis",
         "NodeGroups": [{"NodeGroupId": "0001", "NodeGroupMembers": members}]}]}
    mod.client_for_cluster = lambda cid, svc: ec
    mod.verify_approval = lambda *a, **k: {"ok": True} if guard_ok else {"ok": False, "reason": "denied"}
    return ec


def test_failover_requires_approval_then_executes():
    ec = _wire_failover(failover, replicas=1)
    r1 = failover.test_elasticache_failover_impl(None, cluster_id="my-redis")
    assert r1["status"] == "approval_required"
    r2 = failover.test_elasticache_failover_impl(None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r2["status"] == "ok"
    ec.test_failover.assert_called_once()
    assert ec.test_failover.call_args.kwargs["NodeGroupId"] == "0001"


def test_failover_no_replica_rejected():
    ec = _wire_failover(failover, replicas=0)
    r = failover.test_elasticache_failover_impl(None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r["status"] in ("invalid", "unsupported_engine")
    ec.test_failover.assert_not_called()
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `python -m pytest tests/unit/mcp_servers/operations/test_elasticache_writes.py -q` → FAIL (new modules missing).

- [ ] **Step 3: Create `reboot_elasticache.py`:**

```python
"""reboot_elasticache — approval-gated reboot of the primary cache cluster of a
replication group (reboot_cache_cluster). Brief disruption. Mirrors the write
model; never raises out."""

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster


def _primary_member(client, name):
    rg = (client.describe_replication_groups(ReplicationGroupId=name).get("ReplicationGroups") or [])
    if not rg:
        return None
    members = rg[0].get("MemberClusters") or []
    return members[0] if members else None


def reboot_elasticache_impl(cache, cluster_id=None, approved=False, approval_id=None, **_):
    if not cluster_id:
        return {"status": "invalid", "reason": "cluster_id가 필요합니다"}
    row = lookup_cluster(cluster_id) or {}
    name = row.get("resource_name") or cluster_id
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        member = _primary_member(client, name)
    except Exception as e:
        return {"status": "error", "reason": f"조회 실패: {str(e)[:200]}", "cluster_id": cluster_id}
    if not member:
        return {"status": "error", "reason": "재부팅할 노드를 찾지 못했습니다", "cluster_id": cluster_id}

    payload = {"target": cluster_id}
    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
                "member": member, "warnings": [f"재부팅은 해당 노드({member})를 잠시 중단시킵니다."]}
    guard = verify_approval(approval_id, cluster_id, "reboot_elasticache", payload=payload)
    if not guard.get("ok"):
        return {"status": "approval_denied", "reason": guard.get("reason", "approval guard rejected"), "cluster_id": cluster_id}
    try:
        # resolve node ids for the member cache cluster
        cc = (client.describe_cache_clusters(CacheClusterId=member, ShowCacheNodeInfo=True).get("CacheClusters") or [])
        node_ids = [n.get("CacheNodeId") for n in (cc[0].get("CacheNodes") or [])] if cc else []
        if not node_ids:
            node_ids = ["0001"]
        client.reboot_cache_cluster(CacheClusterId=member, CacheNodeIdsToReboot=node_ids)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        return {"status": "error", "reason": f"reboot_cache_cluster 실패: {code or str(e)[:200]}", "cluster_id": cluster_id}
    return {"status": "ok", "cluster_id": cluster_id, "member": member}
```

- [ ] **Step 4: Create `test_elasticache_failover.py`:**

```python
"""test_elasticache_failover — approval-gated failover test (test_failover) for a
replication group that HAS a replica. No replica → invalid. Mirrors the write
model; never raises out."""

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster


def test_elasticache_failover_impl(cache, cluster_id=None, node_group_id=None,
                                   approved=False, approval_id=None, **_):
    if not cluster_id:
        return {"status": "invalid", "reason": "cluster_id가 필요합니다"}
    row = lookup_cluster(cluster_id) or {}
    rd = row.get("resource_details") or {}
    if (rd.get("engine") or "redis").lower() == "memcached":
        return {"status": "unsupported_engine", "reason": "Memcached는 failover를 지원하지 않습니다", "cluster_id": cluster_id}
    name = row.get("resource_name") or cluster_id
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        rg = (client.describe_replication_groups(ReplicationGroupId=name).get("ReplicationGroups") or [])
    except Exception as e:
        return {"status": "error", "reason": f"조회 실패: {str(e)[:200]}", "cluster_id": cluster_id}
    if not rg:
        return {"status": "error", "reason": "replication group을 찾지 못했습니다", "cluster_id": cluster_id}
    node_groups = rg[0].get("NodeGroups") or []
    # default to the first node group; require a replica (>=2 members) to fail over
    ng = None
    for g in node_groups:
        if node_group_id is None or g.get("NodeGroupId") == node_group_id:
            ng = g
            break
    if not ng:
        return {"status": "invalid", "reason": "node group을 찾지 못했습니다", "cluster_id": cluster_id}
    if len(ng.get("NodeGroupMembers") or []) < 2:
        return {"status": "invalid", "reason": "failover하려면 레플리카가 1개 이상 필요합니다", "cluster_id": cluster_id}
    ngid = ng.get("NodeGroupId")

    payload = {"target": cluster_id, "node_group_id": ngid}
    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
                "node_group_id": ngid, "warnings": [f"failover 테스트는 노드그룹 {ngid}의 프라이머리를 전환합니다."]}
    guard = verify_approval(approval_id, cluster_id, "test_elasticache_failover", payload=payload)
    if not guard.get("ok"):
        return {"status": "approval_denied", "reason": guard.get("reason", "approval guard rejected"), "cluster_id": cluster_id}
    try:
        client.test_failover(ReplicationGroupId=name, NodeGroupId=ngid)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        return {"status": "error", "reason": f"test_failover 실패: {code or str(e)[:200]}", "cluster_id": cluster_id}
    return {"status": "ok", "cluster_id": cluster_id, "node_group_id": ngid}
```

- [ ] **Step 5: Add 2 approval projections** to `_project_payload` in `approval_guard.py`:

```python
    if action_type == "reboot_elasticache":
        return {"target": payload.get("target")}
    if action_type == "test_elasticache_failover":
        return {"target": payload.get("target"), "node_group_id": payload.get("node_group_id")}
```

- [ ] **Step 6: Register the 2 tools** in `handler.py` (imports + `_ENGINE_GATED_TOOLS` `"reboot_elasticache": "elasticache_write"`, `"test_elasticache_failover": "elasticache_write"` + 2 `TOOLS` entries with description "ElastiCache only ... Approval-gated write" + input_schema `cluster_id` [+ `node_group_id` optional for failover] + `approved`/`approval_id`).

- [ ] **Step 7: Add 2 parity entries** to `cdk/tool_definitions.py`.

- [ ] **Step 8: Add the engine-gate test.** Append to `test_elasticache_writes.py` (or the existing operations gate test) a check that each of the 4 tools is in `_ENGINE_GATED_TOOLS` with `elasticache_write`, and (via the handler) a non-ElastiCache cluster → `unsupported_engine`. Mirror the existing docdb/ddb gate test pattern.

- [ ] **Step 9: Run tests.**

Run: `python -m pytest tests/unit/mcp_servers/operations/ -q` → PASS.
Run: `python -m pytest tests/unit -q` → no regression.

- [ ] **Step 10: Commit.**

```bash
git add mcp-servers/mcp_servers/operations/tools/reboot_elasticache.py mcp-servers/mcp_servers/operations/tools/test_elasticache_failover.py mcp-servers/mcp_servers/shared/approval_guard.py mcp-servers/mcp_servers/operations/handler.py cdk/tool_definitions.py tests/unit/mcp_servers/operations/test_elasticache_writes.py
git commit -m "feat(elasticache): approval-gated reboot + failover-test write tools"
```

---

### Task 3: CDK IAM — ElastiCache write actions

**Files:**

- Modify: `cdk/stacks/agent_stack.py` (operations MCP Lambda: add the 4 write actions)

- [ ] **Step 1: Read** the operations MCP Lambda IAM block (the EC-3 commit added `elasticache:DescribeReplicationGroups`/`DescribeCacheClusters`). Add the write actions.

- [ ] **Step 2: Add the write IAM:**

```python
        operations_mcp_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticache:ModifyReplicationGroup",
                "elasticache:CreateSnapshot",
                "elasticache:RebootCacheCluster",
                "elasticache:TestFailover",
            ],
            resources=["*"],
        ))
```

(Mirrors the dynamodb/docdb write grants' `resources=["*"]`. The describes were added in EC-3.)

- [ ] **Step 3: Run synth.**

Run: `python -m pytest tests/cdk/test_synth.py -q` → PASS.

- [ ] **Step 4: Commit.**

```bash
git add cdk/stacks/agent_stack.py
git commit -m "feat(elasticache): operations MCP IAM for write tools (modify/snapshot/reboot/failover)"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD` — focus: every tool is FAIL-CLOSED (no `approved` → `approval_required`; guard-fail → `approval_denied`, NO mutation); the 4 action_types each have a matching `_project_payload` branch (so the approval is payload-bound) AND the tool passes the SAME payload to `verify_approval` that `request_approval` would hash; engine-gated `elasticache_write` (all 4); snapshot/failover correctly refuse Memcached/no-replica; cross-account via `client_for_cluster`; no destructive op; `cdk/tool_definitions.py` parity for all 4; IAM is exactly the 4 write actions.
- Deploy dev: `cdk deploy dbops-dev-agent` (operations MCP Lambda code + IAM). No frontend change.
- Live smoke: the approval→execute happy-path needs an admin token + a real cluster + the Approval Center; **unit-covered** (FAIL-CLOSED + execute paths). A lightweight live check: invoke the deployed tool (direct Lambda invoke, no `approved`) against a registered ElastiCache cluster → `approval_required` (proves the tool + gate + describe path live without mutating). Non-ElastiCache cluster → `unsupported_engine`. (Full approval-consume→mutate is unit-covered, same constraint as the DynamoDB/DocDB write tools.)
- Then `superpowers:finishing-a-development-branch`.
