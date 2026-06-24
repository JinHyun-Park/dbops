# Multi-Team Tenancy T-1 (Foundation + Primary Enforcement) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add DB-backed teams + an additive, default-open cluster-visibility overlay, enforced on the two primary read paths (`/api/clusters` list + `/api/dashboard/*`), plus an admin Teams management API.

**Architecture:** A vendored `tenancy.py` overlay module (copied byte-identical into each api/ package, like `engine_family.py`) computes `visible_cluster_ids(event, items)` → `None` for admins (all), else the set of unassigned clusters (default-open) ∪ clusters whose `team_id` is a team the caller belongs to. Teams + members live in two new DynamoDB tables; cluster→team is an additive `team_id` attribute on the existing `clusters_table`. Admin-gated `/api/admin/teams*` manages it all.

**Tech Stack:** Python 3.12 (api/ Lambdas, boto3 DynamoDB), AWS CDK (Python), Next.js 16 (frontend deferred to T-3).

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in commits (user rule).
- **Default-open, additive, backward-compatible:** a cluster with no `team_id` is visible to everyone; zero teams ⇒ behavior identical to today. The overlay NEVER grants access beyond today — it only removes assigned clusters from non-members.
- **Admins always see all clusters** — `visible_cluster_ids` returns `None`, `cluster_visible` returns `True`.
- **Fail behavior on infra error:** unassigned clusters stay visible (fail-open to current behavior); an assigned cluster whose membership lookup errors is hidden (fail-closed). `my_team_ids` returns an empty set on error, which yields exactly this.
- **`_is_admin`/identity is fail-closed:** no/invalid bearer ⇒ not admin ⇒ restricted set (not `None`).
- **api/ Lambdas cannot share imports** — the overlay is VENDORED (byte-identical copies) with a parity test (mirror `tests/unit/test_engine_family.py`).
- **All DynamoDB scans/queries paginate** (memory gotcha — single scan truncates at 1MB). Use the existing `_scan_all` pattern.
- **New API routes ⇒ regenerate `frontend/public/openapi.json`** (`python tools/openapi_gen.py`) — route-table parity test.
- **CDK-only infra**; tables follow the `dbops-{ENV}-<name>` naming + PAY_PER_REQUEST + point_in_time_recovery + RemovalPolicy.DESTROY pattern (mirror `clusters_table` at `foundation_stack.py:89`).
- Korean copy for user-facing notes; identifiers/tokens verbatim.

---

### Task 1: The `tenancy.py` overlay module + parity + unit tests

**Files:**

- Create: `api/clusters/tenancy.py` (canonical content)
- Create: `api/dashboard/tenancy.py` (byte-identical copy)
- Test: `tests/unit/api/test_tenancy.py` (logic), `tests/unit/api/test_tenancy_parity.py` (copies identical)

**Interfaces:**

- Produces: `is_admin(event) -> bool`, `caller_username(event) -> str`, `my_team_ids(username) -> set[str]`, `visible_cluster_ids(event, cluster_items) -> set[str] | None`, `cluster_visible(event, cluster_item) -> bool`.
- Consumes: env `TEAM_MEMBERS_TABLE`, `TEAM_MEMBERS_BY_USER_INDEX` (Task 2 creates the table; Task 4/5 wire the env).

- [ ] **Step 1: Write the failing test** — `tests/unit/api/test_tenancy.py`:

```python
import importlib.util, os, sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_MOD = Path(__file__).resolve().parents[3] / "api" / "clusters" / "tenancy.py"


def _load():
    spec = importlib.util.spec_from_file_location("tenancy_clusters", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _event(groups=None, username="u-alice", with_bearer=True):
    # minimal JWT: header.payload.sig with base64url payload carrying claims
    import base64, json
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    tok = f"h.{payload}.s" if with_bearer else ""
    headers = {"authorization": f"Bearer {tok}"} if with_bearer else {}
    return {"headers": headers}


ITEMS = [
    {"cluster_id": "c-open"},                       # unassigned
    {"cluster_id": "c-teamA", "team_id": "tA"},
    {"cluster_id": "c-teamB", "team_id": "tB"},
]


def test_admin_sees_all_returns_none():
    t = _load()
    assert t.visible_cluster_ids(_event(groups=["dbops-admin"]), ITEMS) is None


def test_no_groups_is_admin_fallback():
    t = _load()
    # no groups claim at all => single-admin-deploy fallback => admin => None
    assert t.visible_cluster_ids(_event(groups=None), ITEMS) is None


def test_viewer_no_teams_sees_only_unassigned():
    t = _load()
    with patch.object(t, "my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids(_event(groups=["dbops-viewer"]), ITEMS)
    assert vis == {"c-open"}


def test_viewer_in_team_a_sees_open_plus_team_a_not_b():
    t = _load()
    with patch.object(t, "my_team_ids", return_value={"tA"}):
        vis = t.visible_cluster_ids(_event(groups=["dbops-viewer"]), ITEMS)
    assert vis == {"c-open", "c-teamA"}


def test_cluster_visible_unassigned_true_assigned_member_only():
    t = _load()
    ev = _event(groups=["dbops-viewer"])
    assert t.cluster_visible(ev, {"cluster_id": "x"}) is True          # unassigned
    with patch.object(t, "my_team_ids", return_value={"tA"}):
        assert t.cluster_visible(ev, {"cluster_id": "y", "team_id": "tA"}) is True
        assert t.cluster_visible(ev, {"cluster_id": "z", "team_id": "tB"}) is False


def test_cluster_visible_missing_item_is_default_open():
    t = _load()
    assert t.cluster_visible(_event(groups=["dbops-viewer"]), {}) is True


def test_my_team_ids_infra_error_returns_empty(monkeypatch):
    t = _load()
    monkeypatch.setenv("TEAM_MEMBERS_TABLE", "tbl")
    with patch.object(t.boto3, "resource", side_effect=RuntimeError("ddb down")):
        assert t.my_team_ids("u-alice") == set()


def test_no_bearer_not_admin_restricted():
    t = _load()
    with patch.object(t, "my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids(_event(with_bearer=False), ITEMS)
    assert vis == {"c-open"}   # not admin => restricted, only unassigned
```

- [ ] **Step 2: Run it to verify it fails** — `python -m pytest tests/unit/api/test_tenancy.py -q` → FAIL (module missing).

- [ ] **Step 3: Create `api/clusters/tenancy.py`** (canonical):

```python
"""Multi-team cluster-visibility overlay.

VENDORED MODULE — keep byte-identical across all api/*/tenancy.py copies
(tests/unit/api/test_tenancy_parity.py enforces this). api/ Lambdas are
independent packages and cannot share imports, so the overlay is copied, like
engine_family.py.

Default-open: a cluster with no team_id is visible to everyone. A cluster with
a team_id is visible only to members of that team + admins. Admins see all.
On infra error my_team_ids() returns an empty set, which keeps unassigned
clusters visible (fail-open) while hiding assigned clusters (fail-closed).
"""

import base64
import json
import os

import boto3
from boto3.dynamodb.conditions import Key

ADMIN_GROUP = "dbops-admin"


def _decode_jwt_payload(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _claims(event):
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return {}
    return _decode_jwt_payload(auth.split(" ", 1)[1])


def is_admin(event):
    """Mirror api/clusters/handler.py::_is_admin — admin if dbops-admin in
    groups OR no groups at all; fail-closed on missing/invalid bearer."""
    claims = _claims(event)
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and ADMIN_GROUP not in groups:
        return False
    return True


def caller_username(event):
    c = _claims(event)
    return c.get("cognito:username") or c.get("sub") or ""


def my_team_ids(username):
    """Team ids the user belongs to, via the team_members by-user GSI. Empty
    set on no-username / no-table / infra error (caller treats empty as
    'unassigned clusters only')."""
    if not username:
        return set()
    table_name = os.environ.get("TEAM_MEMBERS_TABLE", "")
    index = os.environ.get("TEAM_MEMBERS_BY_USER_INDEX", "by-user")
    if not table_name:
        return set()
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.query(
            IndexName=index,
            KeyConditionExpression=Key("username").eq(username),
        )
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.query(
                IndexName=index,
                KeyConditionExpression=Key("username").eq(username),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        return {it["team_id"] for it in items if it.get("team_id")}
    except Exception as e:
        print(f"[tenancy] my_team_ids failed for {username}: {e}")
        return set()


def visible_cluster_ids(event, cluster_items):
    """None => all clusters (admin). Else the set of cluster_ids the caller may
    see, given already-fetched registry items (each a dict with 'cluster_id'
    and optional 'team_id')."""
    if is_admin(event):
        return None
    teams = my_team_ids(caller_username(event))
    visible = set()
    for it in (cluster_items or []):
        cid = it.get("cluster_id")
        if not cid:
            continue
        team = it.get("team_id")
        if not team:
            visible.add(cid)          # unassigned => default-open
        elif team in teams:
            visible.add(cid)          # assigned to a team I'm in
    return visible


def cluster_visible(event, cluster_item):
    """Single-cluster visibility for per-cluster routes. Admin => True.
    Unassigned (or missing registry item) => True (default-open). Assigned =>
    True iff the caller is a member of the cluster's team."""
    if is_admin(event):
        return True
    team = (cluster_item or {}).get("team_id")
    if not team:
        return True
    return team in my_team_ids(caller_username(event))
```

- [ ] **Step 4: Copy to `api/dashboard/tenancy.py`** — byte-identical (`cp api/clusters/tenancy.py api/dashboard/tenancy.py`).

- [ ] **Step 5: Write the parity test** — `tests/unit/api/test_tenancy_parity.py`:

```python
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_COPIES = [
    _ROOT / "api" / "clusters" / "tenancy.py",
    _ROOT / "api" / "dashboard" / "tenancy.py",
]


def test_tenancy_copies_are_byte_identical():
    contents = [p.read_bytes() for p in _COPIES]
    assert all(c == contents[0] for c in contents), (
        "api/*/tenancy.py copies drifted — keep them byte-identical"
    )
```

- [ ] **Step 6: Run tests** — `python -m pytest tests/unit/api/test_tenancy.py tests/unit/api/test_tenancy_parity.py -q` → PASS.

- [ ] **Step 7: Commit.**

```bash
git add api/clusters/tenancy.py api/dashboard/tenancy.py tests/unit/api/test_tenancy.py tests/unit/api/test_tenancy_parity.py
git commit -m "feat(tenancy): cluster-visibility overlay module (vendored, default-open)"
```

---

### Task 2: CDK — teams + team_members tables + GSI

**Files:**

- Modify: `cdk/stacks/foundation_stack.py` (after `clusters_table`, ~line 100)
- Test: `tests/cdk/test_synth.py` (existing snapshot/synth test must still pass; add an assertion if the test file enumerates expected tables)

**Interfaces:**

- Produces: `self.teams_table`, `self.team_members_table` (with GSI `by-user`) — consumed by Task 3/4/5 CDK wiring.

- [ ] **Step 1: Add the tables** in `foundation_stack.py` immediately after the `clusters_table` block (mirror its config — PAY_PER_REQUEST, point_in_time_recovery, RemovalPolicy.DESTROY):

```python
        # ===== Multi-team tenancy =====
        # teams: one row per team. team_members: one row per (team, member);
        # the by-user GSI answers "which teams is this user in?" in O(1) for the
        # per-request visibility overlay (api/*/tenancy.py).
        self.teams_table = dynamodb.Table(
            self, "TeamsTable",
            table_name=f"dbops-{Settings.ENV}-teams",
            partition_key=dynamodb.Attribute(name="team_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.team_members_table = dynamodb.Table(
            self, "TeamMembersTable",
            table_name=f"dbops-{Settings.ENV}-team-members",
            partition_key=dynamodb.Attribute(name="team_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="username", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.team_members_table.add_global_secondary_index(
            index_name="by-user",
            partition_key=dynamodb.Attribute(name="username", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
```

- [ ] **Step 2: Run synth test** — `python -m pytest tests/cdk/test_synth.py -q` → PASS (synth succeeds with the two new tables + GSI). If the test snapshots resource counts, update the snapshot per its documented refresh command.

- [ ] **Step 3: Commit.**

```bash
git add cdk/stacks/foundation_stack.py tests/cdk/
git commit -m "feat(tenancy): teams + team_members DynamoDB tables (by-user GSI)"
```

---

### Task 3: Admin Teams API + CDK wiring

**Files:**

- Create: `api/admin_teams/handler.py`
- Modify: `cdk/stacks/agent_stack.py` (new `admin_teams` Lambda + routes; mirror the `admin_users`/`clusters` Lambda blocks)
- Modify: `frontend/public/openapi.json` (regen)
- Test: `tests/unit/api/test_admin_teams.py`

**Interfaces:**

- Consumes: `teams_table`, `team_members_table` (Task 2); `clusters_table` (existing) for assign/unassign.
- Produces: routes `GET/POST /api/admin/teams`, `GET/DELETE /api/admin/teams/{team_id}`, `POST/DELETE /api/admin/teams/{team_id}/members/{username}`, `POST/DELETE /api/admin/teams/{team_id}/clusters/{cluster_id}`.

- [ ] **Step 1: Write the failing test** — `tests/unit/api/test_admin_teams.py`. Load the handler via importlib; mock `boto3.resource`/`boto3.client` DynamoDB tables. Assert:
  - viewer (token with `dbops-viewer`) → 403 on every route (GET list, POST create, member add, cluster assign). (The priv-esc fail-closed gate.)
  - admin POST `/api/admin/teams` `{"name":"Team A"}` → 201/200 with a generated `team_id`; a `teams_table.put_item` happened.
  - admin POST `/api/admin/teams/{tid}/members/{username}` → member row written to `team_members_table`.
  - admin POST `/api/admin/teams/{tid}/clusters/{cid}` → `clusters_table.update_item` set `team_id=tid`; DELETE → removes `team_id`.
  - admin DELETE `/api/admin/teams/{tid}` → clears `team_id` on the team's clusters (query/scan paginated) + deletes member rows + the team row.
  - GET `/api/admin/teams` → lists teams with member counts (paginated scan).

```python
import base64, importlib.util, json
from pathlib import Path
from unittest.mock import MagicMock, patch

_MOD = Path(__file__).resolve().parents[3] / "api" / "admin_teams" / "handler.py"


def _load():
    spec = importlib.util.spec_from_file_location("admin_teams_handler", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ev(method, path, groups, path_params=None, body=None):
    claims = {"cognito:username": "u-admin", "cognito:groups": groups}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "headers": {"authorization": f"Bearer h.{payload}.s"},
        "pathParameters": path_params or {},
        "body": json.dumps(body) if body is not None else None,
    }


def test_viewer_forbidden_on_create():
    t = _load()
    r = t.lambda_handler(_ev("POST", "/api/admin/teams", ["dbops-viewer"], body={"name": "X"}))
    assert r["statusCode"] == 403


def test_admin_create_team_writes_row():
    t = _load()
    teams = MagicMock()
    with patch.object(t, "_teams_table", return_value=teams):
        r = t.lambda_handler(_ev("POST", "/api/admin/teams", ["dbops-admin"], body={"name": "Team A"}))
    assert r["statusCode"] in (200, 201)
    assert "team_id" in json.loads(r["body"])
    assert teams.put_item.called


def test_admin_assign_cluster_sets_team_id():
    t = _load()
    clusters = MagicMock()
    with patch.object(t, "_clusters_table", return_value=clusters), \
         patch.object(t, "_teams_table", return_value=MagicMock(**{"get_item.return_value": {"Item": {"team_id": "tA", "name": "A"}}})):
        r = t.lambda_handler(_ev("POST", "/api/admin/teams/tA/clusters/c1",
                                 ["dbops-admin"], path_params={"team_id": "tA", "cluster_id": "c1"}))
    assert r["statusCode"] == 200
    assert clusters.update_item.called
```

(Add the remaining asserts — member add/remove, unassign, delete-team-clears-clusters, list — following the same mock shape. Every route must have a viewer-403 case and an admin-happy case.)

- [ ] **Step 2: Run it to verify it fails** — `python -m pytest tests/unit/api/test_admin_teams.py -q` → FAIL (module missing).

- [ ] **Step 3: Create `api/admin_teams/handler.py`** (mirror `api/admin_users/handler.py` auth helpers + dispatch):

```python
"""Admin Teams & cluster-assignment management API (admin-gated).

Routes:
  GET    /api/admin/teams                                  — list teams
  POST   /api/admin/teams                                  — create {name}
  GET    /api/admin/teams/{team_id}                        — detail (members+clusters)
  DELETE /api/admin/teams/{team_id}                        — delete (unassigns clusters)
  POST   /api/admin/teams/{team_id}/members/{username}     — add member
  DELETE /api/admin/teams/{team_id}/members/{username}     — remove member
  POST   /api/admin/teams/{team_id}/clusters/{cluster_id}  — assign cluster
  DELETE /api/admin/teams/{team_id}/clusters/{cluster_id}  — unassign cluster

Teams gate cluster VISIBILITY (see api/*/tenancy.py); they do not change role.
Admin-gated, fail-closed (mirror api/admin_users/handler.py)."""

import base64
import json
import os
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

ADMIN_GROUP = "dbops-admin"


def _decode_jwt_payload(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_admin(event):
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and ADMIN_GROUP not in groups:
        return False
    return True


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _teams_table():
    return boto3.resource("dynamodb").Table(os.environ["TEAMS_TABLE"])


def _members_table():
    return boto3.resource("dynamodb").Table(os.environ["TEAM_MEMBERS_TABLE"])


def _clusters_table():
    return boto3.resource("dynamodb").Table(os.environ["CLUSTERS_TABLE"])


def _scan_all(table, **kwargs):
    items = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _query_all(table, **kwargs):
    items = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _list_teams():
    teams = _scan_all(_teams_table())
    members = _members_table()
    out = []
    for t in teams:
        tid = t.get("team_id")
        mcount = len(_query_all(members, KeyConditionExpression=Key("team_id").eq(tid)))
        out.append({"team_id": tid, "name": t.get("name"), "created_at": t.get("created_at"),
                    "created_by": t.get("created_by"), "member_count": mcount})
    return out


def _team_detail(tid):
    t = _teams_table().get_item(Key={"team_id": tid}).get("Item")
    if not t:
        return None
    member_rows = _query_all(_members_table(), KeyConditionExpression=Key("team_id").eq(tid))
    members = [m.get("username") for m in member_rows]
    clusters = [c.get("cluster_id") for c in _scan_all(
        _clusters_table(),
        FilterExpression="team_id = :tid",
        ExpressionAttributeValues={":tid": tid},
    )]
    return {"team_id": tid, "name": t.get("name"), "members": members, "clusters": clusters}


def _caller_username(event):
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return ""
    c = _decode_jwt_payload(auth.split(" ", 1)[1])
    return c.get("cognito:username") or c.get("sub") or ""


def lambda_handler(event, context=None):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()
    if method == "OPTIONS":
        return _resp(200, {})
    if not _is_admin(event):
        return _resp(403, {"error": "admin only"})

    pp = event.get("pathParameters") or {}
    tid = pp.get("team_id")
    username = pp.get("username")
    cluster_id = pp.get("cluster_id")
    path = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get("path", "")

    try:
        # ----- member sub-routes -----
        if username and tid:
            if method == "POST":
                if not _teams_table().get_item(Key={"team_id": tid}).get("Item"):
                    return _resp(404, {"error": "team not found"})
                _members_table().put_item(Item={"team_id": tid, "username": username})
                return _resp(200, {"team_id": tid, "username": username, "member": True})
            if method == "DELETE":
                _members_table().delete_item(Key={"team_id": tid, "username": username})
                return _resp(200, {"team_id": tid, "username": username, "member": False})
            return _resp(405, {"error": "method not allowed"})

        # ----- cluster assignment sub-routes -----
        if cluster_id and tid:
            if method == "POST":
                if not _teams_table().get_item(Key={"team_id": tid}).get("Item"):
                    return _resp(404, {"error": "team not found"})
                _clusters_table().update_item(
                    Key={"cluster_id": cluster_id},
                    UpdateExpression="SET team_id = :t",
                    ExpressionAttributeValues={":t": tid},
                )
                return _resp(200, {"cluster_id": cluster_id, "team_id": tid})
            if method == "DELETE":
                _clusters_table().update_item(
                    Key={"cluster_id": cluster_id},
                    UpdateExpression="REMOVE team_id",
                )
                return _resp(200, {"cluster_id": cluster_id, "team_id": None})
            return _resp(405, {"error": "method not allowed"})

        # ----- team-level routes -----
        if tid:
            if method == "GET":
                d = _team_detail(tid)
                return _resp(200, d) if d else _resp(404, {"error": "team not found"})
            if method == "DELETE":
                # unassign every cluster pointing at this team, then drop members + team
                for c in _scan_all(_clusters_table(), FilterExpression="team_id = :t",
                                   ExpressionAttributeValues={":t": tid}):
                    _clusters_table().update_item(
                        Key={"cluster_id": c["cluster_id"]}, UpdateExpression="REMOVE team_id")
                for m in _query_all(_members_table(), KeyConditionExpression=Key("team_id").eq(tid)):
                    _members_table().delete_item(Key={"team_id": tid, "username": m["username"]})
                _teams_table().delete_item(Key={"team_id": tid})
                return _resp(200, {"team_id": tid, "deleted": True})
            return _resp(405, {"error": "method not allowed"})

        # ----- collection routes -----
        if method == "GET":
            return _resp(200, {"teams": _list_teams()})
        if method == "POST":
            try:
                body = json.loads(event.get("body") or "{}")
            except Exception:
                return _resp(400, {"error": "malformed JSON body"})
            name = (body.get("name") or "").strip()
            if not name:
                return _resp(400, {"error": "name required"})
            new_tid = "team-" + uuid.uuid4().hex[:12]
            from datetime import datetime, timezone
            _teams_table().put_item(Item={
                "team_id": new_tid, "name": name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": _caller_username(event),
            })
            return _resp(201, {"team_id": new_tid, "name": name})
        return _resp(405, {"error": "method not allowed"})
    except ClientError as e:
        print(f"[admin_teams] DynamoDB error: {e}")
        return _resp(500, {"error": "operation failed"})
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/unit/api/test_admin_teams.py -q` → PASS.

- [ ] **Step 5: Wire the Lambda + routes in `cdk/stacks/agent_stack.py`** — mirror the `clusters_lambda`/`admin_users` blocks. Add after the admin_users Lambda definition:

```python
        admin_teams_lambda = lambda_.Function(
            self, "AdminTeamsApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/admin_teams"),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "TEAMS_TABLE": foundation.teams_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        foundation.teams_table.grant_read_write_data(admin_teams_lambda)
        foundation.team_members_table.grant_read_write_data(admin_teams_lambda)
        foundation.clusters_table.grant_read_write_data(admin_teams_lambda)

        _admin_teams_int = integrations.HttpLambdaIntegration("AdminTeamsIntegration", admin_teams_lambda)
        for _p in ("/api/admin/teams",
                   "/api/admin/teams/{team_id}",
                   "/api/admin/teams/{team_id}/members/{username}",
                   "/api/admin/teams/{team_id}/clusters/{cluster_id}"):
            self.api.add_routes(
                path=_p,
                methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE],
                integration=_admin_teams_int,
            )
```

(Confirm `apigwv2`/`integrations`/`lambda_` are the import aliases used in the file; match the existing admin_users wiring exactly. Use the same route-registration style the file already uses.)

- [ ] **Step 6: Regenerate openapi + run synth/parity** — `python tools/openapi_gen.py`; `python -m pytest tests/cdk/test_synth.py tests/unit/test_openapi_spec.py -q` → PASS.

- [ ] **Step 7: Commit.**

```bash
git add api/admin_teams/ cdk/stacks/agent_stack.py frontend/public/openapi.json tests/unit/api/test_admin_teams.py
git commit -m "feat(tenancy): admin Teams API (CRUD + members + cluster assignment)"
```

---

### Task 4: Enforce visibility on `/api/clusters` list

**Files:**

- Modify: `api/clusters/handler.py` (`_handle_list` + the GET dispatch at the `lambda_handler`)
- Modify: `cdk/stacks/agent_stack.py` (clusters_lambda env + grant for team_members read)
- Test: `tests/unit/api/test_clusters.py` (extend; create if absent)

**Interfaces:**

- Consumes: `tenancy.visible_cluster_ids` (Task 1), `team_members_table` (Task 2).

- [ ] **Step 1: Write the failing test** — in `tests/unit/api/test_clusters.py`, load the handler; mock `_scan_all`/`_enrich_with_meta` to return three items (`c-open` no team, `c-teamA`/`tA`, `c-teamB`/`tB`). Assert:
  - admin GET → all 3 returned.
  - viewer in team A (patch `tenancy.my_team_ids` → `{"tA"}`) GET → only `c-open` + `c-teamA`.
  - viewer no teams → only `c-open`.

```python
def test_clusters_list_filtered_for_viewer(monkeypatch, clusters_module):
    t = clusters_module
    items = [{"cluster_id": "c-open"}, {"cluster_id": "c-teamA", "team_id": "tA"},
             {"cluster_id": "c-teamB", "team_id": "tB"}]
    monkeypatch.setattr(t, "_scan_all", lambda table, **k: items)
    monkeypatch.setattr(t, "_enrich_with_meta", lambda x: x)
    monkeypatch.setattr(t.tenancy, "my_team_ids", lambda u: {"tA"})
    ev = _viewer_event("GET", "/api/clusters")            # dbops-viewer token
    r = t.lambda_handler(ev, None)
    ids = {c["cluster_id"] for c in json.loads(r["body"])}
    assert ids == {"c-open", "c-teamA"}
```

- [ ] **Step 2: Run it to verify it fails** — `python -m pytest tests/unit/api/test_clusters.py -k filtered -q` → FAIL.

- [ ] **Step 3: Implement** — at the top of `api/clusters/handler.py` add `import tenancy`. Change `_handle_list(table)` to accept the event and filter:

```python
def _handle_list(table, event):
    items = _enrich_with_meta(_scan_all(table))
    visible = tenancy.visible_cluster_ids(event, items)
    if visible is not None:
        items = [c for c in items if c.get("cluster_id") in visible]
    return _resp(200, items, max_age=30)
```

And update the GET dispatch in `lambda_handler` from `return _handle_list(table)` to `return _handle_list(table, event)`.

- [ ] **Step 4: Run tests** — `python -m pytest tests/unit/api/test_clusters.py -q` → PASS.

- [ ] **Step 5: Wire CDK** — in `agent_stack.py`, on the `clusters_lambda` block (~line 764) add to `environment`:

```python
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
```

and after it: `foundation.team_members_table.grant_read_data(clusters_lambda)`.

- [ ] **Step 6: Synth** — `python -m pytest tests/cdk/test_synth.py -q` → PASS.

- [ ] **Step 7: Commit.**

```bash
git add api/clusters/handler.py cdk/stacks/agent_stack.py tests/unit/api/test_clusters.py
git commit -m "feat(tenancy): filter /api/clusters list by caller team visibility"
```

---

### Task 5: Enforce visibility on `/api/dashboard/*` per-cluster routes

**Files:**

- Modify: `api/dashboard/handler.py` (`lambda_handler` — resolve target cluster + 403 if not visible)
- Modify: `cdk/stacks/agent_stack.py` (dashboard_lambda env + grant for team_members read)
- Test: `tests/unit/api/test_dashboard_tenancy.py`

**Interfaces:**

- Consumes: `tenancy.cluster_visible` + `_lookup_cluster` (existing, `api/dashboard/handler.py:103`), `team_members_table` (Task 2).

- [ ] **Step 1: Read** the dashboard `lambda_handler` to find where `cluster_id` is parsed from `pathParameters`/path and where routes branch — every per-cluster route resolves a `cluster_id`. Identify the single choke point (right after `cluster_id` is determined and before data is fetched) to insert the visibility gate. (If routes parse cluster_id in multiple places, add the gate in a small helper `_require_visible(event, cluster_id) -> Optional[resp]` and call it once per per-cluster branch.)

- [ ] **Step 2: Write the failing test** — `tests/unit/api/test_dashboard_tenancy.py`:

```python
def test_dashboard_viewer_blocked_on_other_team_cluster(monkeypatch, dash):
    monkeypatch.setattr(dash, "_lookup_cluster", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    monkeypatch.setattr(dash.tenancy, "my_team_ids", lambda u: {"tA"})
    r = dash.lambda_handler(_viewer_event("GET", "/api/dashboard/c1/overview", {"id": "c1"}), None)
    assert r["statusCode"] == 403


def test_dashboard_viewer_allowed_on_unassigned(monkeypatch, dash):
    monkeypatch.setattr(dash, "_lookup_cluster", lambda cid: {"cluster_id": cid})  # unassigned
    monkeypatch.setattr(dash.tenancy, "my_team_ids", lambda u: set())
    r = dash.lambda_handler(_viewer_event("GET", "/api/dashboard/c1/overview", {"id": "c1"}), None)
    assert r["statusCode"] != 403


def test_dashboard_admin_always_allowed(monkeypatch, dash):
    monkeypatch.setattr(dash, "_lookup_cluster", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    r = dash.lambda_handler(_admin_event("GET", "/api/dashboard/c1/overview", {"id": "c1"}), None)
    assert r["statusCode"] != 403
```

- [ ] **Step 3: Run it to verify it fails** — `python -m pytest tests/unit/api/test_dashboard_tenancy.py -q` → FAIL.

- [ ] **Step 4: Implement** — add `import tenancy` at the top of `api/dashboard/handler.py`, and a gate helper:

```python
def _require_visible(event, cluster_id):
    """Return a 403 response if the caller may not see this cluster, else None.
    Admin/unassigned/member => None (allowed)."""
    if tenancy.cluster_visible(event, _lookup_cluster(cluster_id)):
        return None
    return {
        "statusCode": 403,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"error": "forbidden", "reason": "이 클러스터에 대한 접근 권한이 없습니다."}),
    }
```

Call `forbid = _require_visible(event, cluster_id); if forbid: return forbid` once per per-cluster route, right after `cluster_id` is resolved. (Per the Step-1 reading, place it at the single choke point if there is one.)

- [ ] **Step 5: Run tests** — `python -m pytest tests/unit/api/test_dashboard_tenancy.py -q` → PASS.

- [ ] **Step 6: Wire CDK** — on the `dashboard_lambda` block (~line 650) add to `environment`:

```python
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
```

and `foundation.team_members_table.grant_read_data(dashboard_lambda)`.

- [ ] **Step 7: Full suite + synth** — `python -m pytest tests/unit -q` (no regression) and `python -m pytest tests/cdk/test_synth.py -q` → PASS.

- [ ] **Step 8: Commit.**

```bash
git add api/dashboard/handler.py cdk/stacks/agent_stack.py tests/unit/api/test_dashboard_tenancy.py
git commit -m "feat(tenancy): gate /api/dashboard per-cluster routes by team visibility"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- **Final whole-branch review (opus — security-critical isolation):** the overlay is default-open + additive (zero teams = today's behavior); admins always get `None`/`True`; assigned-cluster-on-error fails closed while unassigned fails open; the clusters list filter + dashboard gate cover their paths; the vendored copies are byte-identical; admin API is fail-closed on every route (viewer 403); all scans paginate; openapi route parity. **Explicitly note: T-1 covers only clusters + dashboard — the other read paths (reports/approvals/cost/etc.) remain platform-wide until T-2, and the agent until T-4.**
- **Deploy dev:** `cdk deploy dbops-dev-foundation dbops-dev-agent` (new tables + admin_teams Lambda + clusters/dashboard env+grants). No frontend change in T-1.
- **Live smoke (viewer + admin tokens):** create a team via `POST /api/admin/teams`; add the e2e viewer as a member; assign one cluster to the team and leave another unassigned. Then: admin `GET /api/clusters` → sees all; viewer `GET /api/clusters` → sees unassigned + the team cluster, NOT a different team's cluster; viewer `GET /api/dashboard/{other-team-cluster}/overview` → 403; viewer on unassigned → 200. Confirm a deployment with NO teams still returns all clusters to a viewer (backward compat). Clean up the test team afterward.
- Then `superpowers:finishing-a-development-branch` (ff-merge to main). T-2 follows.

```

```
