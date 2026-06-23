# Query Rewrite Suggestion + EXPLAIN before/after — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A "리라이팅 제안" action in Query Lab — the agent proposes a rewritten SQL, then the original and proposed are compared via **plan-only EXPLAIN** (estimated cost before/after). Advisory; the proposed SQL is NEVER executed.

**Architecture:** Reuse `streamChat` (agent) for the suggestion + `/api/explain` (gaining a plan-only `analyze=false` mode) for the before/after. The proposed (unverified) SQL is EXPLAINed plan-only (no ANALYZE → no execution). Frontend orchestrates; one small backend flag.

**Tech Stack:** Python Lambda (api/explain), AWS CDK, Next.js 16 (query-lab page + lib helpers), pytest.

## Global Constraints

- **🔴 The agent-proposed (unverified) SQL must NEVER be executed.** `/api/explain` currently runs PG `EXPLAIN (ANALYZE, …)` which EXECUTES the query. The rewrite before/after MUST use the new plan-only mode (`analyze=false` → no ANALYZE → planner estimate only). Both original and proposed are EXPLAINed plan-only for a fair, safe comparison.
- Advisory only — no auto-apply/execution of rewrites (that's a write → approval gate, out of scope).
- Backward-compatible: `/api/explain` with no `analyze` field (or `analyze=true`) behaves EXACTLY as today (ANALYZE). Existing manual EXPLAIN in query-lab is unchanged.
- Keep all existing `/api/explain` guards: SELECT-only, server-side admin RBAC, prefix-strip, max-len. Do NOT relax the admin gate (plan-only still gated, consistent).
- Korean human-facing text (button "리라이팅 제안", advisory banner, before/after labels); DB jargon English (cost, EXPLAIN, plan).
- No new API route → openapi: regenerate (`python3 tools/openapi_gen.py`); the route table is unchanged so `test_openapi_spec` should stay green — run it to confirm.
- Commits: conventional subject; NO `Co-Authored-By: Claude` trailer; no internal-roadmap refs. Frontend prettier hook → `git add -A` + re-commit if it reformats.

---

## File Structure

**Increment 1 — Backend plan-only EXPLAIN**

- Modify: `api/explain/handler.py` — `analyze` flag → plan-only EXPLAIN for PG.
- Modify: `frontend/public/openapi.json` — regenerated (likely no change; confirm parity).
- Test: `tests/unit/api/test_explain.py` (extend or new).

**Increment 2 — Frontend rewrite + before/after**

- Modify: `frontend/src/lib/api-client.ts` — `analyze?` on the explain fetch.
- Create: `frontend/src/lib/query-rewrite.ts` — pure helpers `extractSqlBlock`, `planTotalCost`.
- Modify: `frontend/src/app/query-lab/page.tsx` — `handleRewrite`, before/after panel, button, banner.

---

## Increment 1 — Backend plan-only EXPLAIN

### Task 1: `/api/explain` plan-only `analyze` flag

**Files:** Modify `api/explain/handler.py`; regenerate `frontend/public/openapi.json`; Test `tests/unit/api/test_explain.py`.

**Interfaces:** `POST /api/explain` body gains optional `analyze` (default `true`). `_build_explain_sql(sql, engine, analyze=True)`.

- [ ] **Step 1: Write failing tests** — read the existing explain test (find it: `tests/unit/api/test_explain*.py`; if none, create `tests/unit/api/test_explain.py` mirroring the api test style). Test `_build_explain_sql` directly:

```python
import importlib
h = importlib.import_module("api.explain.handler")  # match repo convention

def test_pg_default_uses_analyze():
    sql = h._build_explain_sql("SELECT 1", "aurora-postgresql")
    assert "ANALYZE" in sql and "FORMAT JSON" in sql

def test_pg_plan_only_omits_analyze():
    sql = h._build_explain_sql("SELECT 1", "aurora-postgresql", analyze=False)
    assert "ANALYZE" not in sql
    assert "BUFFERS" in sql and "FORMAT JSON" in sql  # still structured plan JSON

def test_mysql_never_analyzes_regardless():
    assert "ANALYZE" not in h._build_explain_sql("SELECT 1", "aurora-mysql", analyze=True)
```

- [ ] **Step 2: Run, verify fail** — `python3 -m pytest tests/unit/api/test_explain.py -q` (TypeError: unexpected `analyze`).

- [ ] **Step 3: Implement** in `api/explain/handler.py`:

```python
def _build_explain_sql(sql: str, engine: str, analyze: bool = True) -> str:
    inner = _strip_explain_prefix(sql).rstrip().rstrip(";")
    if engine.startswith("aurora-mysql") or engine == "mysql":
        return f"EXPLAIN FORMAT=JSON {inner}"
    if analyze:
        return f"EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) {inner}"
    return f"EXPLAIN (BUFFERS, VERBOSE, FORMAT JSON) {inner}"
```

In `lambda_handler`, after reading `sql`, read the flag and thread it to the build call:

```python
    analyze = bool(body.get("analyze", True))
    ...
    explain_sql = _build_explain_sql(sql, engine, analyze)   # at the existing build call site
```

(Find the existing `_build_explain_sql(...)` call and add the `analyze` arg. Keep the SELECT-only + admin RBAC gates exactly as-is — plan-only stays admin-gated, consistent.)

- [ ] **Step 4: openapi + tests** — `python3 tools/openapi_gen.py`; `python3 -m pytest tests/unit/api -q` (new explain tests + no regression); `python3 -m pytest tests/unit/test_openapi_spec.py -q` (route table unchanged → should pass).

- [ ] **Step 5: Commit** — add `api/explain/handler.py frontend/public/openapi.json tests/unit/api/test_explain.py` ; `git commit -m "feat(explain): plan-only EXPLAIN mode (analyze flag) for unverified SQL"`

---

## Increment 2 — Frontend rewrite + before/after

### Task 2: query-rewrite helpers + Query Lab rewrite action

**Files:** Create `frontend/src/lib/query-rewrite.ts`; Modify `frontend/src/lib/api-client.ts`, `frontend/src/app/query-lab/page.tsx`.

**Interfaces:** `extractSqlBlock(markdown: string): string | null`; `planTotalCost(plan: unknown): number | null`; the explain fetch gains `analyze?: boolean`.

- [ ] **Step 1: Pure helpers** `frontend/src/lib/query-rewrite.ts`:

````typescript
// First fenced ```sql block (case-insensitive), trimmed; null if none.
export function extractSqlBlock(md: string): string | null {
  const m = md.match(/```sql\s*([\s\S]*?)```/i);
  const sql = m?.[1]?.trim();
  return sql ? sql : null;
}
// PG EXPLAIN FORMAT JSON: array[0].Plan["Total Cost"]. Returns null if not found.
export function planTotalCost(plan: unknown): number | null {
  const root = Array.isArray(plan) ? plan[0] : plan;
  const p = (root as { Plan?: { ["Total Cost"]?: number } } | undefined)?.Plan;
  const c = p?.["Total Cost"];
  return typeof c === "number" ? c : null;
}
````

If the repo has a frontend unit harness, add tests (extract handles no-block/multiple-blocks; planTotalCost handles array/object/missing). If not, skip (build is the gate) — but these are pure so prefer testing if possible.

- [ ] **Step 2: api-client** — find the explain fetch (`fetchExplain`/the `/api/explain` POST around api-client.ts:1281) and add an optional `analyze?: boolean` arg appended to the POST body (omit when undefined so existing callers send no `analyze` → backend defaults true).

- [ ] **Step 3: Query Lab `handleRewrite`** in `app/query-lab/page.tsx` — mirror `handleAnalyze` (page.tsx:300-329):

  - Set a loading state; stream a REWRITE prompt via `streamChat`: ask for a semantically-equivalent rewrite as a `sql` block + 근거 + 주의사항, **한국어**, and (if `explain?.plan` is loaded) include a short plan summary as grounding.
  - In the `onDone` callback: `extractSqlBlock(analysisText)` → `proposedSql`. If present, call the explain fetch with `analyze:false` for BOTH the original SQL and `proposedSql`; store `{beforePlan, afterPlan}` + their `planTotalCost`. Wrap each EXPLAIN in try/catch — a failed/invalid proposed EXPLAIN (or 403 for non-admin) shows a graceful note, never throws; the suggestion text still renders.
  - Add a "리라이팅 제안" button next to the existing analyze/EXPLAIN actions (same styling); cluster-not-selected guard like analyze.

- [ ] **Step 4: before/after panel + banner** — when `beforePlan`/`afterPlan` exist, render a compact compare: 추정 total cost 원본 vs 제안 (+ 개선/악화 % via the two `planTotalCost`s; guard null), and both plans via the existing `PlanTree` component. Above the rewrite output, an advisory banner: "AI 제안 — 실행 전 동등성·성능을 직접 검증하세요 (아래 비교는 실행 없이 planner 추정 cost)". Reuse existing styling; don't alter the existing EXPLAIN/analysis rendering.

- [ ] **Step 5: Build** — `cd frontend && npm run build` → exit 0, `/query-lab` in route list.

- [ ] **Step 6: Commit (mind prettier)** — `git add frontend/src/lib/query-rewrite.ts frontend/src/lib/api-client.ts frontend/src/app/query-lab/page.tsx` ; `git commit -m "feat(query-lab): rewrite suggestion with plan-only EXPLAIN before/after"` (prettier reformat → `git add -A` + re-run).

---

## Self-Review

- Spec §1.2 (plan-only safety — never execute proposed SQL) → Task 1 (`analyze=false`) + Task 3 (`analyze:false` on both EXPLAINs). ✓
- Spec §3.1 (backend analyze flag, guards intact) → Task 1. ✓
- Spec §3.2 (handleRewrite + before/after + helpers) → Task 2. ✓
- Non-breaking: `analyze` defaults true (existing EXPLAIN unchanged); new button/panel additive; helpers pure. ✓
- Safety: proposed SQL only ever hits plan-only EXPLAIN (no ANALYZE/execution); admin gate + SELECT-only retained. ✓
- Type consistency: `analyze` bool end-to-end; `extractSqlBlock`/`planTotalCost` consumed by handleRewrite. ✓
