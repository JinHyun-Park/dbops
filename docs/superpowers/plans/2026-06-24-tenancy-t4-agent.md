# Multi-Team Tenancy T-4 (Agent/Chat SSE Tenancy) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the chat agent tenant-scoped — a non-admin caller can only have the agent operate on clusters they may see (their teams' + unassigned); admins are unrestricted. Two layers: a system-prompt constraint (UX/first-line) and a `BeforeToolCallEvent` hook that hard-blocks a tool call on a non-visible cluster (survives prompt manipulation).

**Architecture:** In `invoke(payload, context)`, decode the inbound Cognito JWT from `context.request_headers["Authorization"]` (AgentCore forwards it; already validated by the Cognito authorizer) → resolve the visible cluster set via a vendored `agent/tenancy.py` (registry scan + team membership). Inject the set into the system prompt (non-admin) and register a Strands hook that cancels any tool call whose `cluster_id` argument isn't visible. Admin → visible is `None` → no constraint + hook no-op.

**Tech Stack:** Python 3.10+ (AgentCore Runtime container, Strands Agents SDK, boto3 DynamoDB).

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in commits.
- **NO `__pycache__` / `.pyc` in `agent/`** — AgentCore Runtime REJECTS artifacts with bytecode caches. Validate `.py` with `ast.parse`, NEVER `py_compile`/`python -c`/exec inside `agent/`. Clean any cache before commit.
- **Admin → no-op:** `visible_cluster_ids_for` returns `None` for admins → no system-prompt constraint, hook allows everything. Identical to today for admins.
- **Default-open / backward-compatible:** unassigned clusters (no `team_id`) stay chat-accessible to everyone; a zero-teams deployment behaves exactly as today.
- **Fail behavior:** no/undecodable `Authorization` → treat as viewer-with-no-teams (visible = unassigned only); registry/membership SCAN error → return `None` (fail-open to current behavior — never break chat on a transient DDB outage). The hook, given `None`, is a no-op.
- **Token never logged** — log `type(e).__name__` only; no token/claims/PII in logs.
- **The hook must never crash the turn** — wrap its logic; on internal error for a non-admin with a cluster_id present, fail-closed (cancel the tool) but return the clean Korean error, never raise into the stream.
- **`agent/tenancy.py` is NOT in the api/ parity set** (different package root) — it gets its OWN focused unit test under `tests/unit/agent/`.
- Korean copy for the refusal/constraint text; identifiers verbatim.

**Grounded API facts (verified — use these exactly):**

- Entry: `@app.entrypoint async def invoke(payload, context)` (`agent/server.py:275`). `context.request_headers` is `Optional[Dict[str,str]]` carrying `Authorization` (`bedrock_agentcore/runtime/app.py:321-324`). Header key casing may vary — check `"Authorization"` and `"authorization"`.
- Strands: `from strands.hooks import HookProvider, HookRegistry`; `from strands.hooks.events import BeforeToolCallEvent`. A `HookProvider` implements `register_hooks(self, registry) -> None` calling `registry.add_callback(BeforeToolCallEvent, self._cb)`. `BeforeToolCallEvent` has `tool_use: ToolUse` (a TypedDict `{input: Any, name: str, toolUseId: str}`) and a WRITABLE `cancel_tool` — set `event.cancel_tool = "<msg>"` to cancel the tool and return an error tool-result. `Agent(..., hooks=[provider])` registers it (`strands/agent/agent.py:225`).
- `build_system_prompt(extra_context: str = "")` at `agent/prompts/system_prompt.py:7`.
- The vendored REST overlay to mirror logic from: `api/clusters/tenancy.py` (`_decode_jwt_payload`, `_claims`, `is_admin`, `caller_username`, `my_team_ids`, `visible_cluster_ids`, `visible_set_from_registry`).
- CDK Runtime: `cdk/stacks/agent_stack.py:519` `self.runtime = agentcore.Runtime(...)` with `environment_variables={...}` (~:533) — has NO `CLUSTERS_TABLE`/`TEAM_MEMBERS_TABLE` today; the Runtime IAM role grant block is just after the construct.

---

### Task 1: `agent/tenancy.py` — identity → visible set

**Files:**

- Create: `agent/tenancy.py`
- Test: `tests/unit/agent/test_agent_tenancy.py` (create `tests/unit/agent/__init__.py` if the dir is new)

**Interfaces:**

- Produces: `visible_cluster_ids_for(headers: dict) -> set[str] | None` (`None` = admin/all), plus internal `_claims_from_headers`, `_is_admin`, `_my_team_ids`.

- [ ] **Step 1: Write the failing test** — `tests/unit/agent/test_agent_tenancy.py`. Load `agent/tenancy.py` via importlib; mock `boto3`. Build a header dict with a base64url JWT payload (mirror the api/ tenancy test's `_event`). Assert:

  - admin headers (groups `["dbops-admin"]`) → `None`.
  - no groups claim → `None` (single-admin fallback).
  - viewer (`["dbops-viewer"]`) in team A (mock `_my_team_ids`→`{"tA"}`; mock the CLUSTERS_TABLE scan → `[{cluster_id:c-open}, {cluster_id:c-teamA,team_id:tA}, {cluster_id:c-teamB,team_id:tB}]`) → `{"c-open","c-teamA"}`.
  - viewer no teams → `{"c-open"}`.
  - no/empty `Authorization` header → viewer-no-teams behavior → `{"c-open"}` (unassigned only) — i.e. NOT `None`, NOT all.
  - CLUSTERS_TABLE scan raises → `None` (fail-open).

- [ ] **Step 2: Run → fail** (`python -m pytest tests/unit/agent/test_agent_tenancy.py -q`).

- [ ] **Step 3: Implement `agent/tenancy.py`** (adapt the api/ overlay; header-based identity + fail-open-on-scan-error):

```python
"""Agent-side cluster-visibility overlay. Mirrors api/clusters/tenancy.py logic
but resolves identity from the inbound request headers (AgentCore forwards the
Cognito Authorization header) instead of an API Gateway event. Returns the set
of cluster_ids the caller may see, or None for admins (no restriction)."""

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


def _claims_from_headers(headers):
    headers = headers or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return {}
    return _decode_jwt_payload(auth.split(" ", 1)[1])


def _is_admin(claims):
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and ADMIN_GROUP not in groups:
        return False
    return True


def _my_team_ids(username):
    if not username:
        return set()
    table_name = os.environ.get("TEAM_MEMBERS_TABLE", "")
    index = os.environ.get("TEAM_MEMBERS_BY_USER_INDEX", "by-user")
    if not table_name:
        return set()
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.query(IndexName=index, KeyConditionExpression=Key("username").eq(username))
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.query(IndexName=index, KeyConditionExpression=Key("username").eq(username),
                               ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return {it["team_id"] for it in items if it.get("team_id")}
    except Exception as e:
        print(f"[agent.tenancy] my_team_ids failed: {type(e).__name__}")
        return set()


def visible_cluster_ids_for(headers):
    """None => admin / all clusters (no restriction). Else the set of cluster_ids
    the caller may see (unassigned + their teams'). No/undecodable token => a
    non-admin with no teams (unassigned only). Registry-scan failure => None
    (fail-open to current behavior; never break chat on a transient DDB outage)."""
    claims = _claims_from_headers(headers)
    if _is_admin(claims):
        return None
    username = claims.get("cognito:username") or claims.get("sub") or ""
    teams = _my_team_ids(username)
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        return None
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.scan(ProjectionExpression="cluster_id, team_id")
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.scan(ProjectionExpression="cluster_id, team_id",
                              ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
    except Exception as e:
        print(f"[agent.tenancy] registry scan failed: {type(e).__name__}")
        return None
    visible = set()
    for it in items:
        cid = it.get("cluster_id")
        if not cid:
            continue
        team = it.get("team_id")
        if not team or team in teams:
            visible.add(cid)
    return visible
```

- [ ] **Step 4: Run → pass.** Confirm no `__pycache__` left in `agent/` (`find agent -name __pycache__` → empty; remove if present).

- [ ] **Step 5: Commit.** `git add agent/tenancy.py tests/unit/agent/ && git commit -m "feat(tenancy): agent-side visible-cluster overlay from request headers"`

---

### Task 2: the `BeforeToolCallEvent` gate hook

**Files:**

- Create: `agent/tool_gate.py`
- Test: `tests/unit/agent/test_tool_gate.py`

**Interfaces:**

- Consumes: `BeforeToolCallEvent` (Strands). Produces: `class ClusterVisibilityGate(HookProvider)` constructed with `visible: set[str] | None`.

- [ ] **Step 1: Read** `strands/hooks/events.py` (`BeforeToolCallEvent` — confirm `tool_use["input"]` holds the arguments + `cancel_tool` is settable) and `strands/hooks/registry.py` (`HookProvider.register_hooks` + `registry.add_callback`). Adjust the import path if `BeforeToolCallEvent` lives under `strands.experimental.hooks` in this version (try `from strands.hooks.events import BeforeToolCallEvent`, fall back to `from strands.experimental.hooks import BeforeToolCallEvent`).

- [ ] **Step 2: Write the failing test** — `tests/unit/agent/test_tool_gate.py`. Build a fake event object with `tool_use={"name":"get_top_queries","input":{"cluster_id":"c-teamB"}}` and a settable `cancel_tool` attribute (a simple stand-in class). Assert:

  - gate with `visible={"c-open","c-teamA"}` + a tool_use on `c-teamB` → `event.cancel_tool` is set to the Korean denial string.
  - gate on `c-teamA` (visible) → `cancel_tool` NOT set.
  - tool_use with NO `cluster_id` (e.g. a fleet/doc tool) → not set.
  - gate with `visible=None` (admin) → never set, even for `c-teamB`.
  - tool_use input missing/malformed → not set (no crash).

- [ ] **Step 3: Run → fail.**

- [ ] **Step 4: Implement `agent/tool_gate.py`:**

```python
"""Strands BeforeToolCall hook: hard-block a tool call whose cluster_id is not
in the caller's visible set. The system-prompt constraint is advisory; THIS is
the guarantee that survives prompt manipulation — the tool never executes on a
cluster the caller can't see."""

try:
    from strands.hooks.events import BeforeToolCallEvent
except ImportError:  # older/experimental layout
    from strands.experimental.hooks import BeforeToolCallEvent  # type: ignore
from strands.hooks import HookProvider, HookRegistry

_DENY = "이 클러스터에 대한 접근 권한이 없습니다."


class ClusterVisibilityGate(HookProvider):
    """visible: a set of allowed cluster_ids, or None for admin/unrestricted."""

    def __init__(self, visible):
        self._visible = visible

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool)

    def _before_tool(self, event) -> None:
        if self._visible is None:
            return  # admin / unrestricted
        try:
            tool_use = getattr(event, "tool_use", None) or {}
            args = tool_use.get("input") or {}
            cid = args.get("cluster_id") if isinstance(args, dict) else None
        except Exception:
            return  # can't parse — let it through (fleet/doc tools have no cluster_id)
        if not cid:
            return  # no cluster scope on this tool
        if cid not in self._visible:
            event.cancel_tool = _DENY
```

- [ ] **Step 5: Run → pass.** No `__pycache__` in `agent/`.

- [ ] **Step 6: Commit.** `git add agent/tool_gate.py tests/unit/agent/test_tool_gate.py && git commit -m "feat(tenancy): BeforeToolCall hook blocks non-visible cluster tool calls"`

---

### Task 3: wire into `invoke` + system prompt + CDK

**Files:**

- Modify: `agent/server.py` (`invoke`), `agent/prompts/system_prompt.py` (`build_system_prompt`), `cdk/stacks/agent_stack.py` (Runtime env + IAM)
- Test: `tests/unit/agent/test_system_prompt.py`; `ast.parse` check on `agent/server.py`

**Interfaces:** Consumes Task 1 `visible_cluster_ids_for` + Task 2 `ClusterVisibilityGate`.

- [ ] **Step 1: Write the failing test** — `tests/unit/agent/test_system_prompt.py`: `build_system_prompt("", visible_clusters={"c-open","c-teamA"})` → the result contains the constraint heading + both ids; `build_system_prompt("", visible_clusters=None)` → NO constraint block (byte-identical to the no-arg base prompt + extra_context). Run → fail.

- [ ] **Step 2: Extend `build_system_prompt`** (`agent/prompts/system_prompt.py`):

```python
def build_system_prompt(extra_context: str = "", visible_clusters=None) -> str:
    # ... existing base prompt assembly ...
    prompt = _BASE  # whatever it currently returns (keep existing logic)
    if extra_context:
        prompt += extra_context  # keep existing placement
    if visible_clusters is not None:
        ids = ", ".join(sorted(visible_clusters)) if visible_clusters else "(없음)"
        prompt += (
            "\n\n## 접근 제한 (테넌시)\n"
            f"당신은 다음 클러스터에만 접근할 수 있습니다: {ids}.\n"
            "이 목록에 없는 클러스터에 대한 질문이나 작업 요청은 정중히 거절하고, "
            "해당 클러스터에 대한 접근 권한이 없다고 한국어로 안내하세요. "
            "목록에 없는 cluster_id로 도구를 호출하지 마세요."
        )
    return prompt
```

(Preserve the EXACT existing base-prompt + extra_context handling — only append the new block when `visible_clusters is not None`.)

- [ ] **Step 3: Wire `invoke`** (`agent/server.py`): add `import tenancy` + `from tool_gate import ClusterVisibilityGate` (match the sibling-import style the file uses — it does `from prompts.system_prompt import build_system_prompt` with an `agent.` fallback; mirror that for `tenancy`/`tool_gate`). In `invoke`, before building the Agent:

```python
    headers = getattr(context, "request_headers", None) or {}
    try:
        visible = tenancy.visible_cluster_ids_for(headers)
    except Exception as e:
        log.warning(f"tenancy resolve failed: {type(e).__name__}")
        visible = None  # fail-open — never break chat
```

and change the Agent construction (`server.py:331`) to:

```python
        agent = Agent(
            model=model,
            system_prompt=build_system_prompt(_load_context_files(), visible_clusters=visible),
            tools=tools,
            hooks=[ClusterVisibilityGate(visible)],
        )
```

- [ ] **Step 4: CDK** (`agent_stack.py`): add to the Runtime `environment_variables` (~:533):

```python
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
```

and after the Runtime construct, grant its IAM role read (find the Runtime role — `self.runtime.role` or the construct's role attribute; mirror the existing `context_files_table` grant just below the construct):

```python
        foundation.clusters_table.grant_read_data(self.runtime.role)
        foundation.team_members_table.grant_read_data(self.runtime.role)
```

(Confirm the correct role accessor by reading how the existing `CONTEXT_FILES_TABLE` grant is written right after the construct — use the SAME accessor.)

- [ ] **Step 5: Validate + test.** `python -c "import ast; ast.parse(open('agent/server.py').read())"` → OK (do NOT import it — no AWS at import). `python -m pytest tests/unit/agent -q` → PASS. `python -m pytest tests/cdk/test_synth.py -q` → PASS. Confirm NO `__pycache__` under `agent/` before commit (`find agent -name __pycache__ -exec rm -rf {} +`).

- [ ] **Step 6: Commit.** `git add agent/server.py agent/prompts/system_prompt.py cdk/stacks/agent_stack.py tests/unit/agent/ && git commit -m "feat(tenancy): enforce cluster visibility in the chat agent (prompt + tool gate)"`

---

## Post-implementation (controller, after all tasks reviewed clean)

- **Final whole-branch review (opus — security-critical, the last isolation surface):** the agent resolves identity from the validated inbound header; admin → None → no constraint + hook no-op; the `BeforeToolCallEvent` hook hard-blocks a non-visible `cluster_id` (verify it actually cancels, not just logs); default-open (unassigned chat-accessible); fail behavior (no-token → unassigned-only; scan-error → fail-open None); NO `__pycache__` in the artifact; the system-prompt block only appears for non-admins; CDK grants are read-only + scoped. Confirm the program-completion claim (REST + UI + agent all tenant-scoped) is now true and not over-stated.
- **Deploy dev:** `cdk deploy dbops-dev-agent` (Runtime container rebuild + env + IAM). **NOTE: AgentCore env/container changes take effect on the next cold start — the warm container can serve the OLD code for up to ~10 min; wait/verify before smoke** (memory: AgentCore env change ~10min warm-container delay).
- **Live smoke (viewer token + seeded team, mirror T-1/T-2):** seed a team with the e2e viewer as member + one assigned cluster + one OTHER-team cluster (data-rich, e.g. pgtsd). Drive an SSE chat invoke as the viewer (replicate `frontend/src/lib/agentcore-sse.ts`: POST the Runtime invoke URL with the Cognito token + session-id header + `{"prompt": "[cluster: <other-team-cluster>]\n이 클러스터의 느린 쿼리를 보여줘"}`). Confirm the agent REFUSES / the tool is blocked (no data returned for the hidden cluster). Then the same for a VISIBLE cluster → it proceeds. Admin token (if available) → unrestricted. Clean up the test team. If a full SSE drive is impractical, at minimum confirm the deployed Runtime has the new env (`aws bedrock-agentcore ... ` or via the CDK output) + the unit-level guarantees, and note the limitation honestly.
- Then `superpowers:finishing-a-development-branch` (ff-merge to main). **This completes the multi-team-tenancy program (T-1..T-4).**
