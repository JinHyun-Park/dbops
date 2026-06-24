# ElastiCache Cost-Explorer View (`?view=elasticache`) — Design

**Date:** 2026-06-24
**Status:** approved (EC-5 follow-up; the deferred Cost-tab piece. Pure mirror of the existing `?view=rds` Cost-Explorer view.)

## Context

EC-5 shipped node-resize cost SIMULATION + a right-sizing finding. The "Cost 탭"
piece the user originally listed — actual ElastiCache SPEND from Cost Explorer —
was deferred as a separate CE-query surface. This spec adds it as a new
`?view=elasticache` arm of the existing `/api/cost` handler + a frontend Cost-page
tab, a near-exact mirror of the already-shipped `?view=rds` view.

## Architecture

### Backend — `api/cost/handler.py`

Mirror `_handle_rds_view` (the RDS/Aurora CE view):

1. **`_elasticache_services(ce, start, end)`** — mirror `_rds_services`: enumerate
   `SERVICE` dimension values whose name (lower-cased) contains `"elasticache"`;
   fall back to a canonical default `["Amazon ElastiCache"]` on failure/empty.
2. **`_handle_elasticache_view(ce, start, end, days)`** — mirror `_handle_rds_view`:
   - `_query_total(ce, start, end, services)` (no tag filter — the customer's own
     ElastiCache spend; DBOps doesn't tag customer clusters).
   - `_query_by_dimension(..., "USAGE_TYPE")` → spend by node-hours / data-transfer /
     backup-storage usage types.
   - `_query_per_cluster(...)` → per-cluster spend IF a cost-allocation tag is
     active (same tag candidates the RDS view tries); else the same
     `per_cluster_note` activation guidance.
   - `_detect_anomalies(daily)` (the shared z-score spike detector).
   - Return the SAME envelope as the RDS view with `"view": "elasticache"`.
3. **Dispatch** in `lambda_handler`: add `if view == "elasticache": return
_handle_elasticache_view(ce, start, end, days)` alongside the existing
   rds/platform/tokens arms.

No new IAM (Cost Explorer `ce:GetCostAndUsage`/`GetDimensionValues` already granted
for the rds/platform/bedrock views). No tag filter (account-wide ElastiCache spend).

### Frontend — `frontend/src/app/cost/page.tsx`

- Extend `CostTab` union with `"elasticache"`; add an "ElastiCache" tab.
- Add the `view: "elasticache"` response type (same shape as the rds view type).
- Fetch on tab select (mirror the rds fetch). Render: total, daily trend, by
  usage-type table, per-cluster (or the activation note), anomalies — reusing the
  existing rds-view rendering components (the views are shape-identical, so the
  same table/chart/empty-state components apply). A `usageLabel` for ElastiCache
  usage types (mirror `rdsUsageLabel`) for readable rows (NodeUsage, etc.).
- Korean copy for descriptions/notes; usage-type/service tokens verbatim.

## Data Flow

Browser Cost page (ElastiCache tab) → `GET /api/cost?view=elasticache&days=N` →
Cost Explorer (SERVICE=ElastiCache, no tag filter) → total + by-usage-type +
per-cluster(if tagged) + anomalies → rendered with the existing rds-view components.

## Error Handling

- CE failure → the same graceful envelope the rds view returns (total 0, empty
  daily, `no_data_reason` explaining CE activation / 24h delay).
- per-cluster unavailable (tag not activated) → `per_cluster_available: false` +
  the activation-guidance note (mirror rds).

## Testing

- **Backend unit** (extend `tests/unit/api/test_cost*.py` if present, else create):
  `_elasticache_services` filters SERVICE values containing "elasticache" + falls
  back to the default; `_handle_elasticache_view` returns the envelope with
  `view: "elasticache"` (mock the CE client like the existing rds-view tests);
  the dispatch routes `view=elasticache`.
- **Frontend**: `npm run build` clean; the ElastiCache tab renders.
- Full unit suite + CDK synth green (no infra change).

## Security

- Read-only Cost Explorer reads (existing IAM). Account-wide ElastiCache spend
  (no tag filter) — same exposure as the rds view (admin/authz-gated dashboard).
  No new permissions, no mutation.
