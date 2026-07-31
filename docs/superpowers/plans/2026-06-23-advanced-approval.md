# Advanced Approval (Designated Approvers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin define approval policies that route approval rights for specific clusters/actions to designated approvers, and prevent self-approval — with a safe fallback to today's "any admin" rule when no policy matches.

**Architecture:** A `dbops-{env}-approval-policies` DynamoDB table (FoundationStack); a pure `resolve_eligible_approvers()` matching function + enforcement in the existing `api/approvals/handler.py` PUT approve flow; an admin-only fail-closed CRUD API (`api/approval_policies/handler.py`) with agent-stack routes; an admin-only management UI hidden from viewers.

**Tech Stack:** AWS CDK (Python), DynamoDB, Lambda (Python 3.12), API Gateway HTTP API, Next.js 16 (static export).

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **OpenAPI parity:** any new route requires `python tools/openapi_gen.py` regen; `tests/unit/test_openapi_spec.py` enforces it.
- **CDK-only infra:** all AWS resources via CDK stacks. No AWS CLI/console changes.
- **Admin gate is server-side + fail-closed:** copy the hardened `api/config/handler.py` `_is_admin` exactly — `if not auth.lower().startswith("bearer "): return False`; `claims = _decode_jwt_payload(...)`; `if not claims: return False`; groups not a list → False; `dbops-viewer` without `dbops-admin` → False; else True. UI nav gating is cosmetic; the API enforces.
- **Enforcement is additive + fallback:** a matched policy narrows approval to (admin AND in the designated set AND ≠ requester); no match → current behavior (any admin). Self-approval is always prevented. Both new checks apply to `action == "approve"` only; `reject` keeps the `_is_admin`-only gate.
- **Fail-safe enforcement:** a policy-table read error in the approve path is swallowed → eligible set empty → fallback to any-admin. Never block the approval loop on policy-infra failure. Self-approval prevention does not depend on the table.
- **Approver identity** is matched case-insensitively: stored approvers are trimmed + lower-cased; the caller identity is `_caller_name(event)` lower-cased.
- **Korean UI copy** for explanatory/empty-state text; keep DBA-known English jargon. Match the Settings page styling + the project quality bar (no AI-generated feel).

---

### Task 1: Foundation — `approval-policies` DynamoDB table + grant helpers

**Files:**

- Modify: `cdk/stacks/foundation_stack.py` (add table after the `app_config_table` block; add grant helpers after `grant_app_config_write`)
- Test: `tests/cdk/test_synth.py` (add a focused assertion)

**Interfaces:**

- Produces: `FoundationStack.approval_policies_table` (a `dynamodb.Table`); `FoundationStack.grant_approval_policy_read(fn)` and `FoundationStack.grant_approval_policy_write(fn)` — each sets `APPROVAL_POLICIES_TABLE` env on `fn` and grants read (or read/write).

- [ ] **Step 1: Add the table.** In `cdk/stacks/foundation_stack.py`, immediately after the `self.app_config_table = dynamodb.Table(...)` block (the App Config table added in a prior feature), add:

```python
        # ===== Approval Policies — designated-approver routing =====
        # Admin-defined policies that restrict WHO may approve specific
        # cluster/action requests (advanced approval). Lives in foundation so
        # the policy CRUD API and the approvals API (both agent stack) reach it
        # via grant helpers without a cross-stack cycle. A request with no
        # matching policy falls back to the existing "any admin" rule, so an
        # empty table reproduces today's behavior.
        self.approval_policies_table = dynamodb.Table(
            self, "ApprovalPoliciesTable",
            table_name=f"dbops-{Settings.ENV}-approval-policies",
            partition_key=dynamodb.Attribute(name="policy_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
```

- [ ] **Step 2: Add the grant helpers.** After the `grant_app_config_write` method, add:

```python
    def grant_approval_policy_read(self, fn) -> None:
        """Wire a Lambda to READ approval policies (env + read grant). Used by
        the approvals API to resolve a request's eligible approver set."""
        fn.add_environment("APPROVAL_POLICIES_TABLE", self.approval_policies_table.table_name)
        self.approval_policies_table.grant_read_data(fn)

    def grant_approval_policy_write(self, fn) -> None:
        """Wire a Lambda to READ/WRITE approval policies (env + R/W grant). Used
        by the policy CRUD API, admin-gated in the handler."""
        fn.add_environment("APPROVAL_POLICIES_TABLE", self.approval_policies_table.table_name)
        self.approval_policies_table.grant_read_write_data(fn)
```

- [ ] **Step 3: Add a synth assertion.** In `tests/cdk/test_synth.py`, after the existing `test_app_config_table_present` test, add:

```python
def test_approval_policies_table_present(cdk_app):
    """Foundation must define the approval-policies table (policy_id PK)."""
    from aws_cdk.assertions import Template

    original_cwd = os.getcwd()
    os.chdir(CDK_DIR)
    try:
        import aws_cdk as cdk_lib
        from config.settings import Settings  # type: ignore  # noqa: F401
        from stacks.foundation_stack import FoundationStack

        app = cdk_lib.App()
        foundation = FoundationStack(app, "test-foundation")
        template = Template.from_stack(foundation)
    finally:
        os.chdir(original_cwd)

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"KeySchema": [{"AttributeName": "policy_id", "KeyType": "HASH"}]},
    )
```

(Mirror whatever shape `test_app_config_table_present` already uses for synthesizing the foundation stack — match it exactly; the block above mirrors the in-app-config feature's test.)

- [ ] **Step 4: Run synth tests.**

Run: `python -m pytest tests/cdk/test_synth.py -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add cdk/stacks/foundation_stack.py tests/cdk/test_synth.py
git commit -m "feat(approval): approval-policies DynamoDB table + grant helpers (foundation)"
```

---

### Task 2: Policy CRUD API — handler + routes + OpenAPI

**Files:**

- Create: `api/approval_policies/handler.py`
- Create: `api/approval_policies/__init__.py` (empty)
- Modify: `cdk/stacks/agent_stack.py` (add `ApprovalPoliciesApi` Lambda near the other API lambdas ~line 772; add routes near the approvals routes ~line 1531)
- Modify: `frontend/public/openapi.json` (regenerated)
- Test: `tests/unit/api/test_approval_policies.py`

**Interfaces:**

- Consumes: `FoundationStack.grant_approval_policy_write` (Task 1).
- Produces: `GET /api/approval-policies` → `{"policies": [policy, ...]}`; `POST /api/approval-policies` body `{cluster_id?, action_type?, approvers[], description?}` → created policy; `PUT /api/approval-policies/{id}` → updated policy; `DELETE /api/approval-policies/{id}` → `{deleted: id}`. A `policy` = `{policy_id, cluster_id, action_type, approvers, description, updated_at, updated_by}`. All admin-gated + fail-closed.

- [ ] **Step 1: Write the handler.** Create `api/approval_policies/handler.py`:

```python
"""Approval-policies API — admin-defined designated-approver routing.

Routes:
  GET    /api/approval-policies        — list all policies
  POST   /api/approval-policies        — create (generates policy_id)
  PUT    /api/approval-policies/{id}    — update
  DELETE /api/approval-policies/{id}    — delete

A policy = {policy_id, cluster_id, action_type, approvers[], description,
updated_at, updated_by}. cluster_id / action_type are an exact value or "*".
approvers are emails/usernames, stored trimmed + lower-cased. Admin-only and
fail-closed (same gate as api/config/handler.py).
"""

import base64
import json
import os
import time
import uuid

import boto3


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _claims(event: dict) -> dict:
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return {}
    return _decode_jwt_payload(auth.split(" ", 1)[1])


def _caller_name(event: dict) -> str:
    c = _claims(event)
    return c.get("preferred_username") or c.get("cognito:username") or c.get("email") or "unknown"


def _is_admin(event: dict) -> bool:
    # Fail-closed (mirrors api/config/handler.py): no parseable "Bearer <jwt>"
    # or empty claims is NOT admin; only a valid token without a viewer-only
    # group claim is admin (one-admin dev fallback).
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
    if "dbops-viewer" in groups and "dbops-admin" not in groups:
        return False
    return True


def _resp(status: int, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _table():
    return boto3.resource("dynamodb").Table(os.environ["APPROVAL_POLICIES_TABLE"])


def _scan_all(table) -> list:
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _validate(body) -> tuple:
    """Return (policy_fields, error). policy_fields excludes policy_id/updated_*."""
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    cluster_id = str(body.get("cluster_id") or "*").strip() or "*"
    action_type = str(body.get("action_type") or "*").strip() or "*"
    raw_approvers = body.get("approvers")
    if not isinstance(raw_approvers, list):
        return None, "approvers must be a list"
    approvers = sorted({str(a).strip().lower() for a in raw_approvers if str(a).strip()})
    if not approvers:
        return None, "approvers must contain at least one non-empty entry"
    description = str(body.get("description") or "").strip()
    return {
        "cluster_id": cluster_id,
        "action_type": action_type,
        "approvers": approvers,
        "description": description,
    }, None


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

    table = _table()
    policy_id = (event.get("pathParameters") or {}).get("id")

    if method == "GET":
        return _resp(200, {"policies": _scan_all(table)})

    if method in ("POST", "PUT"):
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "malformed JSON body"})
        fields, err = _validate(body)
        if err:
            return _resp(400, {"error": err})
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        who = _caller_name(event)
        if method == "PUT":
            if not policy_id:
                return _resp(400, {"error": "policy id required"})
            if "Item" not in table.get_item(Key={"policy_id": policy_id}):
                return _resp(404, {"error": "policy not found"})
        else:
            policy_id = str(uuid.uuid4())
        item = {"policy_id": policy_id, **fields, "updated_at": now, "updated_by": who}
        table.put_item(Item=item)
        return _resp(200 if method == "PUT" else 201, item)

    if method == "DELETE":
        if not policy_id:
            return _resp(400, {"error": "policy id required"})
        table.delete_item(Key={"policy_id": policy_id})
        return _resp(200, {"deleted": policy_id})

    return _resp(405, {"error": f"method {method} not allowed"})
```

- [ ] **Step 2: Add the empty package marker.** Create `api/approval_policies/__init__.py` (empty).

- [ ] **Step 3: Write the tests.** Create `tests/unit/api/test_approval_policies.py`:

```python
"""Tests for the approval_policies API handler."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "approval_policies" / "handler.py"
_spec = importlib.util.spec_from_file_location("approval_policies_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _jwt(admin=True) -> str:
    payload = {"preferred_username": "alice", "cognito:groups": ["dbops-admin"] if admin else ["dbops-viewer"]}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, body=None, path_id=None, admin=True, bearer=True):
    auth = f"Bearer {_jwt(admin=admin)}" if bearer else _jwt(admin=admin)
    e = {
        "requestContext": {"http": {"method": method}},
        "headers": {"authorization": auth},
        "pathParameters": {"id": path_id} if path_id else {},
    }
    if body is not None:
        e["body"] = json.dumps(body)
    return e


def _fake_table(stored=None):
    store = dict(stored or {})
    t = MagicMock()
    t.scan.return_value = {"Items": list(store.values())}
    t.get_item.side_effect = lambda Key: ({"Item": store[Key["policy_id"]]} if Key["policy_id"] in store else {})
    t.put_item.side_effect = lambda Item: store.__setitem__(Item["policy_id"], Item)
    t.delete_item.side_effect = lambda Key: store.pop(Key["policy_id"], None)
    t._store = store
    return t


def test_viewer_denied_on_every_method():
    for m in ("GET", "POST", "PUT", "DELETE"):
        r = handler.lambda_handler(_event(m, body={}, path_id="x", admin=False))
        assert r["statusCode"] == 403


def test_no_bearer_denied():
    r = handler.lambda_handler(_event("GET", bearer=False))
    assert r["statusCode"] == 403


def test_post_creates_and_normalizes():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={
            "cluster_id": "prod-1", "action_type": "execute_sql",
            "approvers": ["  Senior@x.com ", "lead@x.com"], "description": "prod sql",
        }))
    assert r["statusCode"] == 201
    item = json.loads(r["body"])
    assert item["approvers"] == ["lead@x.com", "senior@x.com"]  # trimmed, lowered, sorted
    assert item["policy_id"]
    assert table._store[item["policy_id"]]["cluster_id"] == "prod-1"


def test_post_empty_approvers_rejected():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={"approvers": []}))
    assert r["statusCode"] == 400
    assert table._store == {}


def test_post_defaults_wildcards():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={"approvers": ["a@x.com"]}))
    item = json.loads(r["body"])
    assert item["cluster_id"] == "*" and item["action_type"] == "*"


def test_get_lists():
    table = _fake_table({"p1": {"policy_id": "p1", "cluster_id": "*", "action_type": "*", "approvers": ["a@x.com"]}})
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("GET"))
    assert r["statusCode"] == 200
    assert len(json.loads(r["body"])["policies"]) == 1


def test_put_updates_existing():
    table = _fake_table({"p1": {"policy_id": "p1", "cluster_id": "*", "action_type": "*", "approvers": ["a@x.com"]}})
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", body={"approvers": ["b@x.com"]}, path_id="p1"))
    assert r["statusCode"] == 200
    assert table._store["p1"]["approvers"] == ["b@x.com"]


def test_put_missing_404():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", body={"approvers": ["b@x.com"]}, path_id="nope"))
    assert r["statusCode"] == 404


def test_delete_removes():
    table = _fake_table({"p1": {"policy_id": "p1"}})
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("DELETE", path_id="p1"))
    assert r["statusCode"] == 200
    assert table._store == {}
```

- [ ] **Step 4: Run the handler tests.**

Run: `python -m pytest tests/unit/api/test_approval_policies.py -q`
Expected: PASS (all patch `_table`, no real AWS).

- [ ] **Step 5: Add the CDK Lambda + routes.** In `cdk/stacks/agent_stack.py`, after the `approvals_lambda` block (after its grants/policy, ~line 783), add:

```python
        # Approval-policies API — admin CRUD over designated-approver policies
        # that the approvals API enforces. Admin-gated + fail-closed in-handler.
        approval_policies_lambda = lambda_.Function(
            self, "ApprovalPoliciesApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/approval_policies"),
            timeout=cdk.Duration.seconds(15),
        )
        foundation.grant_approval_policy_write(approval_policies_lambda)  # R/W + env
```

Then near the approvals route registrations (~line 1540, after the `/api/approvals/{id}` route), add:

```python
        # Approval policies — admin-gated designated-approver routing
        approval_policies_integration = integrations.HttpLambdaIntegration(
            "ApprovalPoliciesIntegration", approval_policies_lambda
        )
        self.api.add_routes(
            path="/api/approval-policies",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=approval_policies_integration,
        )
        self.api.add_routes(
            path="/api/approval-policies/{id}",
            methods=[apigwv2.HttpMethod.PUT, apigwv2.HttpMethod.DELETE],
            integration=approval_policies_integration,
        )
```

- [ ] **Step 6: Regenerate OpenAPI.**

Run: `python tools/openapi_gen.py`
Expected: prints an updated count including `/api/approval-policies`.

- [ ] **Step 7: Run parity + synth.**

Run: `python -m pytest tests/unit/api/test_approval_policies.py tests/unit/test_openapi_spec.py tests/cdk/test_synth.py -q`
Expected: PASS.

- [ ] **Step 8: Commit.**

```bash
git add api/approval_policies cdk/stacks/agent_stack.py frontend/public/openapi.json tests/unit/api/test_approval_policies.py
git commit -m "feat(approval): admin-gated CRUD /api/approval-policies + routes + openapi"
```

---

### Task 3: Enforcement — matching function + designated/self-approval checks in approvals PUT

**Files:**

- Modify: `api/approvals/handler.py` (add `resolve_eligible_approvers` + `_load_eligible_approvers`; insert checks in the PUT approve flow)
- Modify: `cdk/stacks/agent_stack.py` (grant `approvals_lambda` read on the policies table)
- Test: `tests/unit/api/test_approval_enforcement.py`

**Interfaces:**

- Consumes: `FoundationStack.grant_approval_policy_read` (Task 1); the `approval-policies` table rows shape `{cluster_id, action_type, approvers[]}` (Task 2).
- Produces: `resolve_eligible_approvers(cluster_id, action_type, policies) -> set[str]` (pure); enforcement that returns `403` for self-approval and for a non-designated approver on a matched policy.

- [ ] **Step 1: Write the matching-function tests.** Create `tests/unit/api/test_approval_enforcement.py`:

```python
"""Tests for advanced-approval matching + enforcement in api/approvals/handler.py."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "approvals" / "handler.py"
_spec = importlib.util.spec_from_file_location("approvals_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

R = handler.resolve_eligible_approvers


def test_exact_cluster_action_wins_over_wildcards():
    policies = [
        {"cluster_id": "*", "action_type": "*", "approvers": ["broad@x.com"]},
        {"cluster_id": "prod-1", "action_type": "execute_sql", "approvers": ["senior@x.com"]},
    ]
    assert R("prod-1", "execute_sql", policies) == {"senior@x.com"}


def test_wildcard_only_matches():
    policies = [{"cluster_id": "*", "action_type": "*", "approvers": ["broad@x.com"]}]
    assert R("any", "any", policies) == {"broad@x.com"}


def test_tie_unions_approvers():
    # Two policies at the same specificity (both cluster-exact, action wildcard)
    policies = [
        {"cluster_id": "prod-1", "action_type": "*", "approvers": ["a@x.com"]},
        {"cluster_id": "prod-1", "action_type": "*", "approvers": ["b@x.com"]},
    ]
    assert R("prod-1", "modify_parameter", policies) == {"a@x.com", "b@x.com"}


def test_no_match_empty():
    policies = [{"cluster_id": "prod-2", "action_type": "*", "approvers": ["a@x.com"]}]
    assert R("prod-1", "execute_sql", policies) == set()
```

- [ ] **Step 2: Run, expect FAIL (function not defined).**

Run: `python -m pytest tests/unit/api/test_approval_enforcement.py -q`
Expected: FAIL (`AttributeError: ... resolve_eligible_approvers`).

- [ ] **Step 3: Add the matching function + loader.** In `api/approvals/handler.py`, after the `_scan_all` helper (~line 90), add:

```python
def resolve_eligible_approvers(cluster_id, action_type, policies) -> set:
    """Return the designated-approver set for a request (lower-cased), or an
    EMPTY set when no policy matches (= policy not applicable → fallback).

    A policy matches when its cluster_id is the request's cluster_id or "*",
    AND its action_type is the request's action_type or "*". The most specific
    matching policy wins (cluster-exact=2 + action-exact=1); ties at the top
    score union their approvers."""
    best, eligible = -1, set()
    for p in policies or []:
        pc = p.get("cluster_id", "*")
        pa = p.get("action_type", "*")
        if pc not in (cluster_id, "*") or pa not in (action_type, "*"):
            continue
        score = (2 if pc == cluster_id else 0) + (1 if pa == action_type else 0)
        approvers = {str(a).strip().lower() for a in (p.get("approvers") or []) if str(a).strip()}
        if score > best:
            best, eligible = score, set(approvers)
        elif score == best:
            eligible |= approvers
    return eligible


def _load_eligible_approvers(cluster_id, action_type) -> set:
    """Resolve the eligible approver set from the policies table. FAIL-SAFE:
    any error (no env, no grant, DDB down) → empty set → fallback to any-admin.
    A policy-infra failure must never freeze the approval loop."""
    try:
        name = os.environ.get("APPROVAL_POLICIES_TABLE")
        if not name:
            return set()
        table = boto3.resource("dynamodb").Table(name)
        return resolve_eligible_approvers(cluster_id, action_type, _scan_all(table))
    except Exception as e:  # noqa: BLE001 - fail-safe by design
        print(f"[approvals] policy load failed: {type(e).__name__}: {e}")
        return set()
```

Confirm `import os` and `import boto3` are already present at the top of `api/approvals/handler.py` (they are — the handler already uses both). If `boto3` is not imported, add it.

- [ ] **Step 4: Run matching tests, expect PASS.**

Run: `python -m pytest tests/unit/api/test_approval_enforcement.py -q`
Expected: PASS (the 4 matching tests).

- [ ] **Step 5: Insert enforcement into the PUT approve flow.** In `api/approvals/handler.py`, find the PUT block. After the `_is_admin` 403 check and after `action = body.get("action")` is validated to be `"approve"`/`"reject"`, and BEFORE the `_scan_all(... approval_id ...)` lookup that fetches `item`, the code already loads `item`. Insert the new checks immediately AFTER `item = items[0]` and BEFORE the `try: table.update_item(... pending→approved ...)`:

```python
        # Advanced approval — designated approvers + separation of duties.
        # Applies to approve only; reject keeps the _is_admin-only gate so a
        # requester can still cancel their own request.
        if action == "approve":
            approver = _caller_name(event)
            if approver and approver == item.get("requested_by"):
                return {
                    "statusCode": 403,
                    "headers": headers,
                    "body": json.dumps({
                        "error": "self_approval",
                        "reason": "자기 요청은 승인할 수 없습니다 — 다른 승인자가 처리해야 합니다.",
                    }),
                }
            action_type = item.get("action_type") or item.get("tool_name")
            eligible = _load_eligible_approvers(item.get("cluster_id"), action_type)
            if eligible and approver.strip().lower() not in eligible:
                return {
                    "statusCode": 403,
                    "headers": headers,
                    "body": json.dumps({
                        "error": "not_designated_approver",
                        "reason": "이 작업은 지정된 승인자만 승인할 수 있습니다.",
                    }),
                }
```

(The exact anchor: this block goes between `item = items[0]` and the comment `# pending 상태에서만 전이 허용` that precedes the `try: table.update_item(...)`. Use the surrounding lines to place it precisely.)

- [ ] **Step 6: Add enforcement tests** to `tests/unit/api/test_approval_enforcement.py` (append). The approvals `lambda_handler` builds its DDB table inline as `table = boto3.resource("dynamodb").Table(os.environ["APPROVALS_TABLE"])` (no `_table()` helper — do NOT add one). So set `APPROVALS_TABLE` and patch `handler.boto3` so the inline table is a mock; patch `handler._scan_all` (the approval-row lookup) and `handler._load_eligible_approvers` (the policy resolution). Append a module-level env default near the top of the file (after the handler import):

```python
import os as _os
_os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
```

Then the tests:

```python
def _jwt(user="alice", admin=True) -> str:
    payload = {"preferred_username": user, "cognito:groups": ["dbops-admin"] if admin else ["dbops-viewer"]}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _put_event(approval_id, approver="alice", action="approve"):
    return {
        "requestContext": {"http": {"method": "PUT"}},
        "headers": {"authorization": f"Bearer {_jwt(user=approver)}"},
        "pathParameters": {"id": approval_id},
        "body": json.dumps({"action": action}),
    }


def _approval_row(requested_by="agent", cluster_id="prod-1", action_type="execute_sql"):
    return {
        "approval_id": "a1", "created_at": "1781069757421",
        "requested_by": requested_by, "approval_status": "pending",
        "cluster_id": cluster_id, "action_type": action_type,
    }


def test_self_approval_denied():
    row = _approval_row(requested_by="alice")
    with patch.object(handler, "boto3"), \
         patch.object(handler, "_scan_all", return_value=[row]), \
         patch.object(handler, "_load_eligible_approvers", return_value=set()):
        r = handler.lambda_handler(_put_event("a1", approver="alice"))
    assert r["statusCode"] == 403
    assert json.loads(r["body"])["error"] == "self_approval"


def test_non_designated_approver_denied():
    row = _approval_row(requested_by="agent")
    with patch.object(handler, "boto3"), \
         patch.object(handler, "_scan_all", return_value=[row]), \
         patch.object(handler, "_load_eligible_approvers", return_value={"senior@x.com"}):
        r = handler.lambda_handler(_put_event("a1", approver="alice"))
    assert r["statusCode"] == 403
    assert json.loads(r["body"])["error"] == "not_designated_approver"


def test_designated_approver_allowed():
    row = _approval_row(requested_by="agent")
    with patch.object(handler, "boto3"), \
         patch.object(handler, "_scan_all", return_value=[row]), \
         patch.object(handler, "_load_eligible_approvers", return_value={"alice"}):
        r = handler.lambda_handler(_put_event("a1", approver="alice"))
    assert r["statusCode"] == 200


def test_no_policy_falls_back_to_any_admin():
    row = _approval_row(requested_by="agent")
    with patch.object(handler, "boto3"), \
         patch.object(handler, "_scan_all", return_value=[row]), \
         patch.object(handler, "_load_eligible_approvers", return_value=set()):
        r = handler.lambda_handler(_put_event("a1", approver="alice"))
    assert r["statusCode"] == 200
```

Why this works: for the two 403 cases the handler returns before any `table.update_item`, so no AWS call. For the 200 cases the inline `table` is a `handler.boto3` mock, so `update_item` is a no-op MagicMock (no `ConditionalCheckFailedException`); `action_type="execute_sql"` skips the `enable_data_api` branch, so the handler returns 200. `_caller_name` resolves `approver` from the token's `preferred_username`. If reading the live handler reveals it routes the lookup differently (e.g. a different helper than `_scan_all` for the `approval_id` fetch), align the patch target to whatever the PUT path actually calls — keep `handler.boto3` patched regardless.

- [ ] **Step 7: Run enforcement tests.**

Run: `python -m pytest tests/unit/api/test_approval_enforcement.py -q`
Expected: PASS (4 matching + 4 enforcement = 8).

- [ ] **Step 8: Grant the policies-table read.** In `cdk/stacks/agent_stack.py`, right after `foundation.clusters_table.grant_read_data(approvals_lambda)` (~line 772), add:

```python
        foundation.grant_approval_policy_read(approvals_lambda)  # designated-approver enforcement
```

- [ ] **Step 9: Run the affected suite + synth.**

Run: `python -m pytest tests/unit/api tests/cdk/test_synth.py -q`
Expected: PASS (no regression in the existing approvals tests).

- [ ] **Step 10: Commit.**

```bash
git add api/approvals/handler.py cdk/stacks/agent_stack.py tests/unit/api/test_approval_enforcement.py
git commit -m "feat(approval): enforce designated approvers + block self-approval"
```

---

### Task 4: Admin management UI — policy page + nav + api-client

**Files:**

- Modify: `frontend/src/lib/api-client.ts` (add types + 4 functions)
- Create: `frontend/src/app/approval-policies/page.tsx`
- Modify: `frontend/src/components/app-shell.tsx` (nav entry, `adminOnly: true`)
- Modify: `frontend/src/components/design-system/command-palette.tsx` (entry, `adminOnly: true`)

**Interfaces:**

- Consumes: `GET/POST/PUT/DELETE /api/approval-policies` (Task 2); `authedFetch` + `apiUrl` + `isAdmin` (existing). The `adminOnly` NavItem field + the `admin` gating already exist in app-shell + command-palette (added by the in-app-config feature).

- [ ] **Step 1: Add the api-client functions.** In `frontend/src/lib/api-client.ts`, add:

```typescript
export interface ApprovalPolicy {
  policy_id: string;
  cluster_id: string;
  action_type: string;
  approvers: string[];
  description: string;
  updated_at?: string;
  updated_by?: string;
}

export async function fetchApprovalPolicies(): Promise<{
  policies: ApprovalPolicy[];
}> {
  const res = await authedFetch(await apiUrl("/api/approval-policies"));
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`policy fetch failed: ${res.status}`);
  return res.json();
}

async function _writePolicy(
  method: "POST" | "PUT",
  body: Partial<ApprovalPolicy>,
  id?: string,
): Promise<ApprovalPolicy> {
  const path = id
    ? `/api/approval-policies/${encodeURIComponent(id)}`
    : "/api/approval-policies";
  const res = await authedFetch(await apiUrl(path), {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) {
    let msg = `policy save failed: ${res.status}`;
    try {
      const b = await res.json();
      if (b?.error) msg = b.error;
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }
  return res.json();
}

export function createApprovalPolicy(
  body: Partial<ApprovalPolicy>,
): Promise<ApprovalPolicy> {
  return _writePolicy("POST", body);
}

export function updateApprovalPolicy(
  id: string,
  body: Partial<ApprovalPolicy>,
): Promise<ApprovalPolicy> {
  return _writePolicy("PUT", body, id);
}

export async function deleteApprovalPolicy(id: string): Promise<void> {
  const res = await authedFetch(
    await apiUrl(`/api/approval-policies/${encodeURIComponent(id)}`),
    {
      method: "DELETE",
    },
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`policy delete failed: ${res.status}`);
}
```

- [ ] **Step 2: Build the management page.** Create `frontend/src/app/approval-policies/page.tsx`. Mirror `frontend/src/app/settings/page.tsx` (read it first) for: `"use client"`, the `PageBody`/`PageHeader`/`Section`/`EmptyState` shell, the `fetchApprovalPolicies()`-on-mount + `"admin only"` → admins-only notice ("이 설정은 관리자만 변경할 수 있습니다."), and loading/error state. Requirements:

  - List existing policies in a table/cards: cluster_id, action_type, approvers (joined), description, updated_by/at.
  - An "정책 추가" form (or inline row) with inputs: cluster_id (default `*`), action_type (default `*`), approvers (one textarea/textfield, comma- or newline-separated → split + trim into a string[]), description. Submit calls `createApprovalPolicy(...)`; on success prepend to the list.
  - Each policy row: an "수정" (edit → PUT via `updateApprovalPolicy`) and "삭제" (delete via `deleteApprovalPolicy`, with a `confirm()` guard) action; update local state from the response/removal.
  - Surface the backend error message (`error.message`) on failure (e.g. "approvers must contain at least one non-empty entry").
  - Korean helper copy: explain `*` wildcard, most-specific-wins, that a matched policy means ONLY listed approvers may approve (admins not listed are blocked), unmatched requests fall back to any admin, and `action_type` matches the request's action_type/tool_name (e.g. `execute_sql`, `modify_parameter`).
  - Match the design quality bar — reuse design-system primitives, no raw-div inconsistency.

- [ ] **Step 3: Add the nav entry.** In `frontend/src/components/app-shell.tsx`, in the `NAV` "Configure" group (where `/settings` lives, added by the in-app-config feature), add after the Settings entry:

```tsx
      {
        href: "/approval-policies",
        label: "Approval policies",
        icon: UserCheck,
        adminOnly: true,
        hint: "지정 승인자 라우팅 — 클러스터·액션별 승인자 (관리자)",
      },
```

Import `UserCheck` from `lucide-react` (add to the existing import block; if `UserCheck` is unavailable in the installed lucide version, use `ShieldCheck` or `UserCog`). The `adminOnly` field + the visible-items filter already exist on `NavItem` (from the in-app-config feature) — no type change needed.

- [ ] **Step 4: Add the command-palette entry.** In `frontend/src/components/design-system/command-palette.tsx`, add to the `commands` array (Configure group), mirroring the existing `settings` entry's `adminOnly: true`:

```tsx
  {
    id: "approval-policies",
    label: "Approval policies — 지정 승인자 라우팅",
    path: "/approval-policies",
    group: "Configure",
    adminOnly: true,
  },
```

- [ ] **Step 5: Build the frontend.**

Run: `cd frontend && npm run build`
Expected: build succeeds (static export), no type errors.

- [ ] **Step 6: Commit.**

```bash
git add frontend/src/lib/api-client.ts frontend/src/app/approval-policies/ frontend/src/components/app-shell.tsx frontend/src/components/design-system/command-palette.tsx
git commit -m "feat(approval): admin UI for designated-approver policies (hidden from viewers)"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD`.
- Deploy dev: `cdk deploy dbops-dev-foundation dbops-dev-agent` (table + grants + both APIs), then frontend build → `aws s3 sync frontend/out/ s3://dbops-dev-frontend-123456789012 --delete --exclude config.json` → CloudFront invalidation `E1234567890ABC`.
- Live smoke (viewer e2e token): policy CRUD viewer → 403 (incl. no-Bearer / garbage → 403); `GET /api/approval-policies` admin shape. Designated-approver enforcement (approve requiring a designated user) needs an admin token + a seeded policy — cover by unit tests + document the live gap honestly.
- Then `superpowers:finishing-a-development-branch`.
