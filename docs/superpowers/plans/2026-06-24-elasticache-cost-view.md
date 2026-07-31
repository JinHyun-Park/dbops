# ElastiCache Cost View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `?view=elasticache` Cost-Explorer view in `/api/cost` + a frontend Cost-page tab showing actual ElastiCache spend — a near-exact mirror of the existing `?view=rds`.

**Architecture:** Add `_elasticache_services` + `_handle_elasticache_view` (mirror `_rds_services`/`_handle_rds_view`) + a dispatch arm; extend the Cost page with an ElastiCache tab. No new IAM, no tag filter.

**Tech Stack:** Python 3.12 (api/cost Lambda, boto3 Cost Explorer), Next.js 16.

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** (user rule).
- Mirror `?view=rds` EXACTLY (same envelope, no tag filter, same per-cluster + anomalies + no_data_reason handling). Only the SERVICE filter ("elasticache") + `"view": "elasticache"` differ.
- Read-only Cost Explorer (existing `ce:GetCostAndUsage`/`GetDimensionValues` IAM — no new grant).
- Korean copy for notes; usage-type/service tokens verbatim.

---

### Task 1: Backend `?view=elasticache`

**Files:**

- Modify: `api/cost/handler.py` (add `_elasticache_services` + `_handle_elasticache_view` + dispatch arm)
- Test: extend the cost handler tests (find `tests/unit/api/test_cost*.py`; if none, create `tests/unit/api/test_cost_elasticache.py`)

**Interfaces:**

- Consumes: `_query_total`, `_query_by_dimension`, `_query_per_cluster`, `_detect_anomalies`, `_response`, `_ENV` (existing). Produces: `_elasticache_services`, `_handle_elasticache_view`.

- [ ] **Step 1: Read the templates.** Read `api/cost/handler.py`: `_rds_services` (~118), `_handle_rds_view` (~220), the `_query_total`/`_query_by_dimension`/`_query_per_cluster` signatures, `_detect_anomalies`, `_response`, and the `lambda_handler` view-dispatch (~443-458) + `_RDS_SERVICE_DEFAULT`.

- [ ] **Step 2: Write the failing test.** Extend/create the cost test (mirror the rds-view test if one exists; else create `tests/unit/api/test_cost_elasticache.py`). Load the handler via importlib; mock the CE client. Assert:

  - `_elasticache_services` keeps SERVICE values whose name contains "elasticache" (case-insensitive) and falls back to the default when CE returns none/errors.
  - `_handle_elasticache_view` returns a 200 response whose body has `view == "elasticache"`, `total`, `daily`, `by_usage_type`, `per_cluster_available`, `anomalies` keys (mock `_query_total`→(daily,total,None), `_query_by_dimension`, `_query_per_cluster` or the CE client beneath them — match how the rds-view test mocks).
  - `lambda_handler` with `queryStringParameters={"view":"elasticache"}` routes to the elasticache view (body `view == "elasticache"`).

  (If no rds-view test exists to mirror, write a minimal one: patch `_query_total`/`_query_by_dimension`/`_query_per_cluster`/`_detect_anomalies` on the module to return canned values and assert the envelope.)

- [ ] **Step 3: Run it to verify it fails.** `python -m pytest tests/unit/api/test_cost_elasticache.py -q` → FAIL.

- [ ] **Step 4: Add `_elasticache_services`** (after `_rds_services`):

```python
_ELASTICACHE_SERVICE_DEFAULT = ("Amazon ElastiCache",)


def _elasticache_services(ce, start, end):
    """SERVICE dimension values that look like ElastiCache. Mirrors _rds_services."""
    keep = []
    try:
        resp = ce.get_dimension_values(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Dimension="SERVICE",
        )
        for v in resp.get("DimensionValues", []):
            name = v.get("Value", "")
            if "elasticache" in name.lower():
                keep.append(name)
    except Exception as e:
        print(f"GetDimensionValues (ElastiCache) failed: {e}")
    if not keep:
        return list(_ELASTICACHE_SERVICE_DEFAULT)
    return keep
```

- [ ] **Step 5: Add `_handle_elasticache_view`** (mirror `_handle_rds_view`, swapping the service discovery + view label + Korean labels to ElastiCache):

```python
def _handle_elasticache_view(ce, start, end, days):
    """ElastiCache spend (Cost Explorer). Same envelope as the RDS view."""
    services = _elasticache_services(ce, start, end)
    daily, total, total_err = _query_total(ce, start, end, services)
    by_usage_type, _ut_err = _query_by_dimension(ce, start, end, services, "USAGE_TYPE")
    per_cluster, cluster_tag, cluster_err = _query_per_cluster(ce, start, end, services)
    per_cluster_available = len(per_cluster) > 0
    per_cluster_note = None
    if not per_cluster_available:
        per_cluster_note = (
            "클러스터별 비용 분리를 사용할 수 없습니다. AWS Billing 콘솔에서 "
            "cost-allocation 태그를 활성화하고 ElastiCache 클러스터에 적용하면 약 "
            "24시간 내에 Cost Explorer가 클러스터 단위로 비용을 분리합니다. 과거 "
            "비용은 소급 반영되지 않습니다."
        )
    no_data_reason = None
    if total == 0 and not daily:
        no_data_reason = (
            "이 기간에 기록된 ElastiCache 비용이 없습니다. ElastiCache를 운영 중이라면 "
            "Cost Explorer 활성화 여부를 확인하세요 (반영까지 약 24시간 지연)."
        )
    anomalies = _detect_anomalies(daily)
    return _response(200, {
        "env": _ENV,
        "view": "elasticache",
        "range_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": round(total, 4),
        "currency": "USD",
        "daily": daily,
        "by_usage_type": by_usage_type,
        "per_cluster": per_cluster,
        "per_cluster_available": per_cluster_available,
        "per_cluster_tag": cluster_tag,
        "per_cluster_note": per_cluster_note,
        "anomalies": anomalies,
        "no_data_reason": no_data_reason,
        "discovered_services": services,
    })
```

- [ ] **Step 6: Add the dispatch arm** in `lambda_handler` (next to the rds/platform arms):

```python
    if view == "elasticache":
        return _handle_elasticache_view(ce, start, end, days)
```

- [ ] **Step 7: Run tests.** `python -m pytest tests/unit/api/ -q` → PASS. `python -m pytest tests/unit -q` → no regression.

- [ ] **Step 8: Commit.**

```bash
git add api/cost/handler.py tests/unit/api/
git commit -m "feat(elasticache): Cost Explorer view (?view=elasticache) mirroring the RDS view"
```

---

### Task 2: Frontend Cost-page ElastiCache tab

**Files:**

- Modify: `frontend/src/app/cost/page.tsx` (CostTab union + tab + view type + fetch + render)
- Modify: `frontend/src/lib/api-client.ts` (if the cost fetch is centralized there; else the page fetches inline — match the existing rds tab)

**Interfaces:**

- Consumes: the `?view=elasticache` response (same shape as the rds view).

- [ ] **Step 1: Read the template.** Read `frontend/src/app/cost/page.tsx`: the `CostTab` union (~82), the rds view type (~63), how a tab is rendered + fetched (the `tab === "rds"` path, `rdsUsageLabel` ~128, `isRds` ~173), and how the fetch is triggered per tab (~152-167). The ElastiCache view is shape-identical to rds — reuse the rds rendering.

- [ ] **Step 2: Add the tab + view.**

  - Extend `CostTab`: `"bedrock" | "rds" | "platform" | "tokens" | "elasticache"`.
  - Add the `view: "elasticache"` response type (clone the rds view type, change the `view` literal).
  - Add an "ElastiCache" tab button next to the RDS tab (same styling).
  - Fetch `?view=elasticache&days=...` on tab select (mirror the rds fetch path).
  - Render with the SAME components the rds tab uses (total/daily/by_usage_type/per_cluster/anomalies) — since the shapes match, treat `tab === "elasticache"` like `tab === "rds"` for rendering (e.g. broaden `isRds`-style guards to include elasticache, or render both via a shared block). Add an `elasticacheUsageLabel` (or reuse a generic usage-label) for readable usage-type rows. Korean tab label/notes.

- [ ] **Step 3: Build.** `cd frontend && npm run build` → PASS, no type errors; `/cost` prerenders.

- [ ] **Step 4: Commit.**

```bash
git add frontend/src/app/cost/page.tsx frontend/src/lib/api-client.ts
git commit -m "feat(elasticache): Cost page ElastiCache spend tab"
```

---

## Post-implementation (controller, after both tasks reviewed clean)

- Final whole-branch review (standard model — small mirror): the elasticache view envelope matches the rds view exactly (only SERVICE filter + view label differ); no tag filter; no new IAM; the dispatch arm doesn't disturb the existing views; the frontend tab reuses the rds rendering without breaking the other tabs.
- Deploy dev: `cdk deploy dbops-dev-agent` (api/cost Lambda). Frontend build → sync → invalidate `E1234567890ABC`.
- Live smoke: `GET /api/cost?view=elasticache&days=30` (viewer token) → 200 with the envelope (`view: "elasticache"`, total/daily/by_usage_type; likely $0 or small in dev — that's fine, the envelope + no_data_reason is the check). Confirm the other views (rds/bedrock/platform/tokens) still return 200 (no regression).
- Then `superpowers:finishing-a-development-branch`.
