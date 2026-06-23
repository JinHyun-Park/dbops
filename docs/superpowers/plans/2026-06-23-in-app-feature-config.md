# In-App Feature Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a DBOps admin toggle opt-in feature settings (ticketing provider, report delivery) from inside the web UI, persisted to DynamoDB, with no redeploy — env vars remain the fallback.

**Architecture:** A `dbops-{env}-app-config` DynamoDB key-value table in FoundationStack; an admin-gated `GET/PUT /api/config` Lambda in AgentStack; a cached `get_config(key, default)` read path wired into `ticketing.get_provider` (task_worker) and `report_generator._deliver_report` with **DB value → env var → default** precedence; an admin-only settings page in the frontend.

**Tech Stack:** AWS CDK (Python), DynamoDB, Lambda (Python 3.12), API Gateway HTTP API, Next.js 16 (static export), TanStack Query / fetch.

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **No internal-roadmap / source references** in commit messages or committed docs — this feature's provenance is confidential. Describe it on its own merits (adoption: enable opt-in features without redeploy).
- **OpenAPI parity:** any new route requires `python tools/openapi_gen.py` regen; `tests/unit/test_openapi_spec.py` enforces it.
- **CDK-only infra:** all AWS resources via CDK stacks. No AWS CLI/console changes.
- **Read precedence:** stored DB value → env var of same name → built-in default. A fresh deploy with zero config rows must behave exactly as today.
- **Fail-safe consumers:** `get_config` must never raise — DDB/permission errors fall back to env/default. Config must never block task completion or report generation.
- **Admin gate server-side:** writes (and reads) use `_is_admin` with the established dev fallback (no Cognito groups ⇒ admin). A `dbops-viewer` is denied.
- **Korean UI copy** for explanatory/empty-state text; keep DBA-known English jargon (provider names, "Report delivery") as-is. Match existing `/preferences` page styling and the project design quality bar (no AI-generated feel).
- **Config holds toggles only, never secrets** — provider credentials stay in Secrets Manager, out of scope.

---

### Task 1: Foundation — `app-config` DynamoDB table + grant helpers

**Files:**

- Modify: `cdk/stacks/foundation_stack.py` (add table after the `agent_tasks_table` GSI block, ~line 159; add grant helpers after `grant_task_manage`, ~line 282)
- Test: `tests/cdk/test_synth.py` (add a focused assertion)

**Interfaces:**

- Produces: `FoundationStack.app_config_table` (a `dynamodb.Table`); `FoundationStack.grant_app_config_read(fn)` and `FoundationStack.grant_app_config_write(fn)` — each sets the `APP_CONFIG_TABLE` env on `fn` and grants read (or read/write) on the table.

- [ ] **Step 1: Add the table.** In `cdk/stacks/foundation_stack.py`, immediately after the `agent_tasks_table` `add_global_secondary_index(... "recency-index" ...)` call (~line 159), add:

```python
        # ===== App Config — in-app, DB-backed feature toggles =====
        # Small key-value store an ADMIN edits from the web UI (GET/PUT
        # /api/config) to flip opt-in features (ticketing provider, report
        # delivery) WITHOUT a redeploy. Lives in foundation so the agent stack
        # (config API + task worker) and the data stack (report generator) can
        # all reach it without a cross-stack cycle — same rationale as the
        # agent_tasks_table above. Read precedence at consumers is
        # DB value -> env var -> default, so a fresh deploy with no rows here
        # behaves exactly as the baked-in env defaults.
        self.app_config_table = dynamodb.Table(
            self, "AppConfigTable",
            table_name=f"dbops-{Settings.ENV}-app-config",
            partition_key=dynamodb.Attribute(name="config_key", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
```

- [ ] **Step 2: Add the grant helpers.** After the `grant_task_manage` method (~line 282), add:

```python
    def grant_app_config_read(self, fn) -> None:
        """Wire a Lambda to READ the app-config table (env + read grant).
        Used by feature consumers (task_worker via ticketing, report_generator)
        to resolve DB-backed toggles with an env/default fallback."""
        fn.add_environment("APP_CONFIG_TABLE", self.app_config_table.table_name)
        self.app_config_table.grant_read_data(fn)

    def grant_app_config_write(self, fn) -> None:
        """Wire a Lambda to READ/WRITE the app-config table (env + R/W grant).
        Used by the config API (GET/PUT /api/config), admin-gated in the handler."""
        fn.add_environment("APP_CONFIG_TABLE", self.app_config_table.table_name)
        self.app_config_table.grant_read_write_data(fn)
```

- [ ] **Step 3: Add a synth assertion.** In `tests/cdk/test_synth.py`, after the existing stack-count/tag tests, add a test that uses `aws_cdk.assertions.Template`. Reuse the `cdk_app` fixture's stacks if it exposes them; otherwise build `Template.from_stack` on the foundation stack the fixture creates. Add:

```python
def test_app_config_table_present(cdk_app):
    """Foundation must define the app-config key-value table (config_key PK)."""
    from aws_cdk.assertions import Template
    foundation = cdk_app["foundation"]  # adapt to how the fixture exposes stacks
    Template.from_stack(foundation).has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [{"AttributeName": "config_key", "KeyType": "HASH"}],
        },
    )
```

If the `cdk_app` fixture returns only the `app` (not a dict of stacks), adapt: read the fixture's return shape first and either extend it to expose `foundation`, or re-synth a fresh `FoundationStack` inside the test (mirroring the fixture's CWD-swap + settings fallback). Do NOT hardcode the env into the table-name assertion — match on `KeySchema` so the test is env-agnostic.

- [ ] **Step 4: Run synth tests.**

Run: `python -m pytest tests/cdk/test_synth.py -q`
Expected: PASS (synth succeeds, new assertion green).

- [ ] **Step 5: Commit.**

```bash
git add cdk/stacks/foundation_stack.py tests/cdk/test_synth.py
git commit -m "feat(config): app-config DynamoDB table + grant helpers (foundation)"
```

---

### Task 2: Config REST API — handler + route + OpenAPI

**Files:**

- Create: `api/config/handler.py`
- Create: `api/config/__init__.py` (empty — match sibling API dirs)
- Modify: `cdk/stacks/agent_stack.py` (add `ConfigApi` Lambda near the other API lambdas ~line 787; add routes near the tasks routes ~line 1539)
- Modify: `frontend/public/openapi.json` (regenerated, do not hand-edit)
- Test: `tests/unit/api/test_config.py`

**Interfaces:**

- Consumes: `FoundationStack.grant_app_config_write` (Task 1).
- Produces: `GET /api/config` → `{"items": [{"key", "value", "default", "updated_at", "updated_by"}]}`; `PUT /api/config` body `{"config": {KEY: value}}` → same shape. Admin-gated.

- [ ] **Step 1: Write the handler.** Create `api/config/handler.py`:

```python
"""App-config API — DB-backed feature toggles an admin edits from the web UI.

Routes:
  GET /api/config   — list all known config keys (stored value or default)
  PUT /api/config   — upsert provided keys (admin-only)

Values are stored as strings in the dbops-{env}-app-config DynamoDB table
(PK config_key). A known-keys allowlist lives here so PUT can't write arbitrary
keys, and each key validates its own value. The API is decoupled from the
ticketing provider registry: TICKETING_PROVIDER validates FORMAT only — an
unwired provider name is inert at runtime (get_provider returns _UnwiredProvider).
"""

import base64
import json
import os
import re
import time

import boto3

# --- known-keys allowlist ---------------------------------------------------
# key -> (default, validator). validator(raw) returns the normalized string
# value to store, or raises ValueError with a human message.


def _v_ticketing_provider(raw) -> str:
    s = str(raw).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", s):
        raise ValueError("TICKETING_PROVIDER must match [a-z0-9_-]{1,32}")
    return s


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _v_bool(raw) -> str:
    if isinstance(raw, bool):
        return "true" if raw else "false"
    s = str(raw).strip().lower()
    if s in _TRUE:
        return "true"
    if s in _FALSE:
        return "false"
    raise ValueError("expected a boolean (true/false)")


CONFIG_KEYS: dict = {
    "TICKETING_PROVIDER": ("none", _v_ticketing_provider),
    "REPORT_DELIVERY_ENABLED": ("false", _v_bool),
}


# --- auth helpers (mirror api/saved_queries/handler.py) ---------------------


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
    return c.get("preferred_username") or c.get("cognito:username") or c.get("email") or "anonymous"


def _is_admin(event: dict) -> bool:
    c = _claims(event)
    groups = c.get("cognito:groups") or []
    if not isinstance(groups, list) or not groups:
        return True  # dev fallback (same as saved_queries/runbooks)
    if "dbops-viewer" in groups and "dbops-admin" not in groups:
        return False
    return True


# --- response + DDB helpers -------------------------------------------------


def _resp(status: int, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,PUT,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _table():
    return boto3.resource("dynamodb").Table(os.environ["APP_CONFIG_TABLE"])


def _read_all() -> dict:
    """Return {config_key: item} for every stored row (allowlisted keys only)."""
    out = {}
    table = _table()
    for key in CONFIG_KEYS:
        got = table.get_item(Key={"config_key": key}).get("Item")
        if got:
            out[key] = got
    return out


def _items_view(stored: dict) -> list:
    """Merge stored rows with defaults into the GET/PUT response shape."""
    items = []
    for key, (default, _validator) in CONFIG_KEYS.items():
        row = stored.get(key) or {}
        items.append({
            "key": key,
            "value": row.get("value", default),
            "default": default,
            "updated_at": row.get("updated_at"),
            "updated_by": row.get("updated_by"),
        })
    return items


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

    if method == "GET":
        return _resp(200, {"items": _items_view(_read_all())})

    if method == "PUT":
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "malformed JSON body"})
        config = body.get("config")
        if not isinstance(config, dict) or not config:
            return _resp(400, {"error": "body must be {\"config\": {KEY: value}}"})

        # validate everything BEFORE writing anything (no partial writes)
        normalized = {}
        for key, raw in config.items():
            if key not in CONFIG_KEYS:
                return _resp(400, {"error": f"unknown config key: {key}"})
            _default, validator = CONFIG_KEYS[key]
            try:
                normalized[key] = validator(raw)
            except ValueError as e:
                return _resp(400, {"error": f"{key}: {e}"})

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        who = _caller_name(event)
        table = _table()
        for key, value in normalized.items():
            table.put_item(Item={
                "config_key": key,
                "value": value,
                "updated_at": now,
                "updated_by": who,
            })
        return _resp(200, {"items": _items_view(_read_all())})

    return _resp(405, {"error": f"method {method} not allowed"})
```

- [ ] **Step 2: Add the empty package marker.** Create `api/config/__init__.py` with no content (match sibling dirs).

- [ ] **Step 3: Write the failing tests.** Create `tests/unit/api/test_config.py`:

```python
"""Tests for the config API handler."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "config" / "handler.py"
_spec = importlib.util.spec_from_file_location("config_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _jwt(user="alice", admin=True) -> str:
    payload = {
        "preferred_username": user,
        "cognito:groups": ["dbops-admin"] if admin else ["dbops-viewer"],
    }
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, body=None, admin=True):
    e = {
        "requestContext": {"http": {"method": method}},
        "headers": {"authorization": f"Bearer {_jwt(admin=admin)}"},
    }
    if body is not None:
        e["body"] = json.dumps(body)
    return e


def _fake_table(stored=None):
    store = dict(stored or {})
    t = MagicMock()
    t.get_item.side_effect = lambda Key: (
        {"Item": store[Key["config_key"]]} if Key["config_key"] in store else {}
    )

    def _put(Item):
        store[Item["config_key"]] = Item
    t.put_item.side_effect = _put
    t._store = store
    return t


def test_get_returns_defaults_when_empty():
    with patch.object(handler, "_table", return_value=_fake_table()):
        r = handler.lambda_handler(_event("GET"))
    assert r["statusCode"] == 200
    items = {i["key"]: i for i in json.loads(r["body"])["items"]}
    assert items["TICKETING_PROVIDER"]["value"] == "none"
    assert items["REPORT_DELIVERY_ENABLED"]["value"] == "false"
    assert items["TICKETING_PROVIDER"]["updated_at"] is None


def test_get_viewer_denied():
    r = handler.lambda_handler(_event("GET", admin=False))
    assert r["statusCode"] == 403


def test_put_persists_and_normalizes_bool():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", {"config": {"REPORT_DELIVERY_ENABLED": True}}))
    assert r["statusCode"] == 200
    assert table._store["REPORT_DELIVERY_ENABLED"]["value"] == "true"
    items = {i["key"]: i for i in json.loads(r["body"])["items"]}
    assert items["REPORT_DELIVERY_ENABLED"]["value"] == "true"
    assert items["REPORT_DELIVERY_ENABLED"]["updated_by"] == "alice"


def test_put_unknown_key_rejected_no_write():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", {"config": {"BOGUS": "x"}}))
    assert r["statusCode"] == 400
    assert table._store == {}


def test_put_bad_provider_format_rejected():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", {"config": {"TICKETING_PROVIDER": "Has Space!"}}))
    assert r["statusCode"] == 400
    assert table._store == {}


def test_put_viewer_denied():
    r = handler.lambda_handler(_event("PUT", {"config": {"REPORT_DELIVERY_ENABLED": True}}, admin=False))
    assert r["statusCode"] == 403
```

- [ ] **Step 4: Run tests, expect FAIL (no env / handler import issues resolved).** Then make pass:

Run: `python -m pytest tests/unit/api/test_config.py -q`
Expected after handler is in place: PASS (the handler reads `_table()` which is patched, so no real AWS).

- [ ] **Step 5: Add the CDK Lambda + routes.** In `cdk/stacks/agent_stack.py`, near the other API lambdas (after `tasks_lambda`/`scheduled_tasks_lambda`, ~line 817), add:

```python
        # Config API — admin-edits DB-backed feature toggles (ticketing
        # provider, report delivery) so they flip without a redeploy.
        config_lambda = lambda_.Function(
            self, "ConfigApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/config"),
            timeout=cdk.Duration.seconds(15),
        )
        foundation.grant_app_config_write(config_lambda)  # R/W + APP_CONFIG_TABLE env
```

Then near the tasks-route registrations (~line 1557), add:

```python
        # App config — admin-gated DB-backed feature toggles
        config_integration = integrations.HttpLambdaIntegration("ConfigIntegration", config_lambda)
        self.api.add_routes(
            path="/api/config",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.PUT],
            integration=config_integration,
        )
```

- [ ] **Step 6: Regenerate OpenAPI.**

Run: `python tools/openapi_gen.py`
Expected: prints an updated path/op count including `/api/config`.

- [ ] **Step 7: Run OpenAPI parity + synth.**

Run: `python -m pytest tests/unit/test_openapi_spec.py tests/cdk/test_synth.py -q`
Expected: PASS.

- [ ] **Step 8: Commit.**

```bash
git add api/config cdk/stacks/agent_stack.py frontend/public/openapi.json tests/unit/api/test_config.py
git commit -m "feat(config): admin-gated GET/PUT /api/config + route + openapi"
```

---

### Task 3: Consumer read path — `get_config` wired into ticketing + report_generator

**Files:**

- Create: `mcp-servers/mcp_servers/shared/app_config.py`
- Modify: `mcp-servers/mcp_servers/workers/ticketing.py` (resolve `None` name via `get_config`)
- Create: `data-pipeline/report_generator/app_config.py` (local copy)
- Modify: `data-pipeline/report_generator/handler.py` (`_deliver_report` gate via `get_config`)
- Modify: `cdk/stacks/agent_stack.py` (grant task_worker config read)
- Modify: `cdk/stacks/data_stack.py` (grant report_generator config read)
- Test: `tests/unit/mcp_servers/shared/test_app_config.py`, extend `tests/unit/mcp_servers/workers/test_ticketing.py` if present (else create a focused test)

**Interfaces:**

- Consumes: `APP_CONFIG_TABLE` env + read grant (`FoundationStack.grant_app_config_read`); the `app-config` table from Task 1.
- Produces: `get_config(key: str, default: str) -> str` — returns the stored DDB value, else `os.environ.get(key)`, else `default`. Caches per-key for ~60s. Never raises.

- [ ] **Step 1: Write the failing test for the shared helper.** Create `tests/unit/mcp_servers/shared/test_app_config.py`:

```python
"""Tests for the shared app_config get_config helper."""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_PKG_ROOT = Path(__file__).resolve().parents[4] / "mcp-servers"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import mcp_servers.shared.app_config as app_config  # noqa: E402


def setup_function():
    app_config._CACHE.clear()  # isolate cache between tests


def _table_with(value):
    t = MagicMock()
    t.get_item.return_value = {"Item": {"config_key": "K", "value": value}} if value is not None else {}
    return t


def test_db_value_wins_over_env_and_default(monkeypatch):
    monkeypatch.setenv("APP_CONFIG_TABLE", "t")
    monkeypatch.setenv("K", "env")
    with patch.object(app_config, "_table", return_value=_table_with("db")):
        assert app_config.get_config("K", "default") == "db"


def test_env_fallback_when_no_row(monkeypatch):
    monkeypatch.setenv("APP_CONFIG_TABLE", "t")
    monkeypatch.setenv("K", "env")
    with patch.object(app_config, "_table", return_value=_table_with(None)):
        assert app_config.get_config("K", "default") == "env"


def test_default_when_no_row_no_env(monkeypatch):
    monkeypatch.setenv("APP_CONFIG_TABLE", "t")
    monkeypatch.delenv("K", raising=False)
    with patch.object(app_config, "_table", return_value=_table_with(None)):
        assert app_config.get_config("K", "default") == "default"


def test_ddb_error_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("APP_CONFIG_TABLE", "t")
    monkeypatch.setenv("K", "env")
    boom = MagicMock()
    boom.get_item.side_effect = RuntimeError("ddb down")
    with patch.object(app_config, "_table", return_value=boom):
        assert app_config.get_config("K", "default") == "env"


def test_no_table_env_uses_fallback(monkeypatch):
    monkeypatch.delenv("APP_CONFIG_TABLE", raising=False)
    monkeypatch.setenv("K", "env")
    assert app_config.get_config("K", "default") == "env"
```

- [ ] **Step 2: Write the shared helper.** Create `mcp-servers/mcp_servers/shared/app_config.py`:

```python
"""Read DB-backed feature config with an env/default fallback.

Resolution precedence for a key:
  1. the stored value in the dbops-{env}-app-config DynamoDB table (admin-set
     via GET/PUT /api/config), if present;
  2. the environment variable of the same name (the deploy-time default);
  3. the caller-supplied default.

Values are cached per-key for a short TTL so the hot path doesn't hit DDB on
every call; a freshly-changed setting takes effect within the TTL on a warm
container. NEVER raises — any DDB/permission error falls back to env/default,
because this gates opt-in features and must not break the work it wraps.
"""

import os
import time

import boto3

_TTL_SECONDS = 60
_CACHE: dict = {}  # key -> (value_or_None, expiry_epoch)


def _table():
    return boto3.resource("dynamodb").Table(os.environ["APP_CONFIG_TABLE"])


def _stored(key: str):
    """Return the stored string value for key, or None. Cached; never raises."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and hit[1] > now:
        return hit[0]
    value = None
    try:
        if os.environ.get("APP_CONFIG_TABLE"):
            item = _table().get_item(Key={"config_key": key}).get("Item")
            if item is not None:
                value = item.get("value")
    except Exception as e:  # noqa: BLE001 - fail-safe by design
        print(f"[app-config] read failed for {key}: {type(e).__name__}: {e}")
        value = None
    _CACHE[key] = (value, now + _TTL_SECONDS)
    return value


def get_config(key: str, default: str) -> str:
    """Resolve key: DB value -> env var of same name -> default."""
    stored = _stored(key)
    if stored is not None:
        return stored
    return os.environ.get(key, default)
```

- [ ] **Step 3: Run the shared-helper tests.**

Run: `python -m pytest tests/unit/mcp_servers/shared/test_app_config.py -q`
Expected: PASS.

- [ ] **Step 4: Wire ticketing.** In `mcp-servers/mcp_servers/workers/ticketing.py`, change the `None`-name branch in `get_provider` (currently `name = os.environ.get("TICKETING_PROVIDER", "none")`, ~line 81) to resolve through config. At the top of the file add the import (near the existing imports):

```python
from mcp_servers.shared.app_config import get_config
```

Then replace the branch:

```python
    if name is None:
        name = get_config("TICKETING_PROVIDER", os.environ.get("TICKETING_PROVIDER", "none"))
```

(Passing the env value as the default keeps behavior identical when `APP_CONFIG_TABLE` is unset — `get_config` returns env/default.)

- [ ] **Step 5: Verify ticketing still passes.** If `tests/unit/mcp_servers/workers/test_ticketing.py` exists, run it; the existing `get_provider()` cases must still pass (with `APP_CONFIG_TABLE` unset, `get_config` falls back to env/"none"). If patching is needed because the test imports trigger a DDB call, patch `mcp_servers.shared.app_config._table` or set no `APP_CONFIG_TABLE` (the helper short-circuits when the env is absent).

Run: `python -m pytest tests/unit/mcp_servers/workers/ -q -k ticketing`
Expected: PASS. If no ticketing test exists, add a minimal one asserting `get_provider()` returns `NoopTicketProvider` when nothing is configured.

- [ ] **Step 6: Add the report_generator local copy.** Create `data-pipeline/report_generator/app_config.py` with the SAME content as Step 2's `mcp-servers/mcp_servers/shared/app_config.py` (verbatim copy — the data-pipeline package shares no layer with mcp-servers, so a small per-Lambda copy follows the existing convention).

- [ ] **Step 7: Wire report_generator.** In `data-pipeline/report_generator/handler.py`, add near the top imports:

```python
from app_config import get_config
```

Then change the `_deliver_report` gate (~line 343) from:

```python
    if os.environ.get("REPORT_DELIVERY_ENABLED", "").strip().lower() not in ("true", "1", "yes", "on"):
        return
```

to:

```python
    enabled = get_config("REPORT_DELIVERY_ENABLED", os.environ.get("REPORT_DELIVERY_ENABLED", "false"))
    if str(enabled).strip().lower() not in ("true", "1", "yes", "on"):
        return
```

- [ ] **Step 8: Grant config read in CDK.** In `cdk/stacks/agent_stack.py`, where `task_worker` is created (the `grant_task_manage(task_worker)` line ~118), add immediately after it:

```python
        foundation.grant_app_config_read(task_worker)  # DB-backed TICKETING_PROVIDER
```

In `cdk/stacks/data_stack.py`, after `self.report_generator` is created and its other grants (the `grant_data_api_access(self.report_generator)` line ~401), add:

```python
        foundation.grant_app_config_read(self.report_generator)  # DB-backed REPORT_DELIVERY_ENABLED
```

- [ ] **Step 9: Run the full affected suite + synth.**

Run: `python -m pytest tests/unit/mcp_servers tests/unit/data_pipeline tests/cdk/test_synth.py -q`
Expected: PASS.

- [ ] **Step 10: Commit.**

```bash
git add mcp-servers/mcp_servers/shared/app_config.py mcp-servers/mcp_servers/workers/ticketing.py data-pipeline/report_generator/app_config.py data-pipeline/report_generator/handler.py cdk/stacks/agent_stack.py cdk/stacks/data_stack.py tests/unit/mcp_servers/shared/test_app_config.py
git commit -m "feat(config): consumers read DB-backed toggles (env fallback + cache)"
```

---

### Task 4: Settings UI — admin page + api-client + nav

**Files:**

- Modify: `frontend/src/lib/api-client.ts` (add `fetchAppConfig`, `updateAppConfig`, types)
- Create: `frontend/src/app/settings/page.tsx`
- Modify: the nav/sidebar component to add a "Settings" entry (find the file that renders the `/preferences` link)

**Interfaces:**

- Consumes: `GET/PUT /api/config` (Task 2); `authedFetch` + `api()`/`apiUrl()` from `api-client.ts`.

- [ ] **Step 1: Add the api-client functions.** In `frontend/src/lib/api-client.ts`, add (mirroring the existing `authedFetch` mutation pattern — PUT carries the Cognito token automatically):

```typescript
export interface AppConfigItem {
  key: string;
  value: string;
  default: string;
  updated_at: string | null;
  updated_by: string | null;
}

export async function fetchAppConfig(): Promise<{ items: AppConfigItem[] }> {
  const res = await authedFetch(await apiUrl("/api/config"));
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`config fetch failed: ${res.status}`);
  return res.json();
}

export async function updateAppConfig(
  config: Record<string, string | boolean>,
): Promise<{ items: AppConfigItem[] }> {
  const res = await authedFetch(await apiUrl("/api/config"), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) {
    let msg = `config update failed: ${res.status}`;
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
```

- [ ] **Step 2: Build the settings page.** Create `frontend/src/app/settings/page.tsx`. Mirror `/preferences` styling (`PageBody`, `PageHeader`, `Section`, `EmptyState` from `@/components/design-system/page-shell`). Requirements:
  - On mount, call `fetchAppConfig()`. On `"admin only"` error, render an admins-only notice (Korean: "이 설정은 관리자만 변경할 수 있습니다.") instead of the form.
  - Render two controls keyed off the returned items:
    - **Report delivery** (`REPORT_DELIVERY_ENABLED`): a toggle (on/off) bound to `value === "true"`.
    - **Ticketing provider** (`TICKETING_PROVIDER`): a text input (or select) for the provider name; default `none`. Include helper copy (Korean) noting that a provider must be wired in code before a non-`none` value does anything.
  - A "저장" (save) button calls `updateAppConfig({ REPORT_DELIVERY_ENABLED: <bool>, TICKETING_PROVIDER: <string> })`, shows success/error, and updates local state from the response.
  - Show `updated_at` / `updated_by` per setting when present ("마지막 변경: {updated_by} · {updated_at}").
  - Match the project design quality bar — no placeholder/AI-generated feel; consistent with existing pages.

Use the `/preferences` page (`frontend/src/app/preferences/page.tsx`) as the structural template for state/loading/error handling.

- [ ] **Step 3: Add the nav entry.** Find the component rendering the `/preferences` nav link (grep `preferences` under `frontend/src/components`), and add a sibling "Settings" link to `/settings`. Match the existing link styling/icon convention.

- [ ] **Step 4: Build the frontend.**

Run: `cd frontend && npm run build`
Expected: build succeeds (static export), no type errors.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/lib/api-client.ts frontend/src/app/settings/ frontend/src/components
git commit -m "feat(config): admin settings page — toggle ticketing + report delivery in-app"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD`.
- Deploy dev: `cdk deploy dbops-dev-foundation dbops-dev-data dbops-dev-agent` (table + grants + API + consumer env), then frontend: `npm run build` → `aws s3 sync frontend/out/ s3://dbops-dev-frontend-830858425797 --delete --exclude config.json` → CloudFront invalidation `E3AHIXF7WMTX01`.
- Live smoke: admin `GET /api/config` returns defaults; `PUT` flips `REPORT_DELIVERY_ENABLED`; `GET` reflects it; viewer gets 403.
- Then `superpowers:finishing-a-development-branch`.
