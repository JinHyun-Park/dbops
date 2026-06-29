# DB Map — Service Blueprint View

**Date:** 2026-06-29
**Status:** Approved (brainstorming → spec)

## Purpose

A single "blueprint" view of every registered DB, grouped by the **service/app it
serves**, so an operator can see the fleet at a glance through a _purpose/topology_
lens (distinct from the Fleet page's _health-triage_ lens). Clicking a DB sets the
global selected cluster and navigates to its dashboard.

## Decisions (from brainstorming)

- **Data source:** auto-infer from existing registry data (engine, region, team,
  status, env-from-name) **+** an admin-editable lightweight note per cluster:
  `purpose` (one line) and `service_tags` (the connected services/apps).
- **Grouping:** by connected service/app (`service_tags`). A DB with multiple tags
  appears under each; untagged DBs go to an **"Unassigned"** group.
- **Form:** a dedicated **`/map` page** (new "Map" nav entry under Monitor) with
  per-service card sections. NOT a node-graph (over-engineered for service→DB
  grouping) and NOT a Fleet tab (keeps health vs purpose lenses separate).

## Data model

Two OPTIONAL fields on the cluster registry item (DynamoDB `clusters` table —
schemaless, so no migration; absent ⇒ no note / Unassigned):

- `purpose: str` — one-line free text.
- `service_tags: list[str]` — connected services/apps (also the grouping key).

## API

- **Read:** reuse the existing `GET /api/clusters` — it returns registry items
  verbatim, so the new fields ride along once set. No new read endpoint.
- **Write:** new `PATCH /api/clusters/{cluster_id}/meta` — admin-gated
  (fail-closed, mirroring the existing `_is_admin` pattern), body
  `{purpose?: str, service_tags?: list[str]}`. Validates: cluster exists,
  `purpose` length cap (e.g. 200 chars), `service_tags` is a list of short
  strings (cap count + length), strips blanks. Updates only the provided fields.
  Requires `python tools/openapi_gen.py` regen + a route in `agent_stack`
  `add_routes` (per the project's per-route registration rule).
- **Env inference:** a pure frontend helper `inferEnv(name, tags)` →
  `prod | staging | dev | null` from the cluster name/tags (display-only; never
  written). Conservative: only tag when a clear token matches.

## Frontend — `/map` page

- New route `frontend/src/app/map/page.tsx` + a "Map" entry in the Monitor nav
  group (`app-shell.tsx`), visible to all roles.
- Reads clusters via `fetchClusters()` (same as other pages). Groups client-side
  by `service_tags`; renders one **section per service** (header = service name +
  DB count), each a responsive grid of DB cards. An "Unassigned" section collects
  DBs with no `service_tags`.
- **Card** (reuses design-system primitives + `engine.ts` badge +
  `lib/cluster-triage.ts` severity so status agrees with Fleet/dashboard):
  engine badge · cluster name · env chip · status dot (triage severity) ·
  region/team · `purpose` note (one line, muted). Whole card is the click target.
- **Click → global select + navigate:** `setSelectedCluster(cluster_id)` (shared
  store) then `router.push('/dashboard?cluster=' + encodeURIComponent(id))`.
- **Admin edit (inline):** admins (`isAdmin()`) get an edit affordance on each
  card opening a small inline editor for `purpose` + `service_tags` → `PATCH`,
  then refetch. Viewers see read-only. The edit control must not hijack the
  card's navigate click (stop propagation).
- Design quality: product-grade, consistent with existing pages; no generic
  AI-slop. Korean for explanatory copy / empty states; English for DB jargon.

## Global selection (verify + fix)

The shared store (`selected-cluster.ts` + `use-selected-cluster.ts`) already does
URL `?cluster=` + localStorage + cross-page live-sync, and the Map click reuses
`setSelectedCluster`. **Verify** with a real click (Map card → dashboard shows the
chosen cluster; header/⌘K reflect it) AND verify the _existing_ dashboard
selection path actually updates the global store; **fix if broken** (the user
flagged it as possibly non-functional).

## Testing

- **Unit:** `inferEnv` (prod/staging/dev/none + no false positives on aliases);
  service-grouping (multi-tag duplication, Unassigned bucket); `PATCH .../meta`
  (admin-gate fail-closed; validation: length/count caps, non-list rejected,
  blank-strip; partial update).
- **Frontend:** Map renders service sections + cards; card click calls
  `setSelectedCluster` + navigates with `?cluster=`; admin edit submits PATCH;
  viewer sees no edit control. Playwright e2e for the click→select→dashboard flow.
- **CDK:** new route present in `tests/cdk/test_synth.py` expectations; openapi
  parity (`test_openapi_spec`) green after regen.

## Out of scope (YAGNI)

- Node-graph / dependency edges (we only have service→DB grouping, not a real
  dependency graph).
- Auto-discovery of "connected service" (DBs don't self-report their apps —
  hence the admin note).
- A separate read endpoint or a new metrics path (reuses `/api/clusters`).
