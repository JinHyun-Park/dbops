# Backup / snapshot visibility for new engines — Design Spec

- **Date**: 2026-06-12
- **Status**: Proposed
- **Depends on**: multi-engine program #1–#5 (deployed). Follow-up from the Codex
  dashboard-parity audit (BACKLOG.md P3.6).

## Goal

Give DocumentDB and DynamoDB dashboards the **backup/snapshot visibility** that
Aurora already has (the `BackupPanel`), **read-only**. This is the highest-value
demoable parity gap: unlike metric-based panels (cost/healthscore/per-GSI) which
are flat on idle demo clusters, backup data is **always present** — the
`dbops-docdb-test` demo cluster has a real automated snapshot + restore window
right now. Snapshot **create/restore** (writes) stay Aurora-only and are deferred
to the NoSQL-write/remediation backlog item.

## Scope

| Engine     | Read (this spec)                                            | Source API                                                      | Write (deferred)                            |
| ---------- | ----------------------------------------------------------- | --------------------------------------------------------------- | ------------------------------------------- |
| Aurora     | already shipped                                             | `rds:DescribeDBClusterSnapshots`                                | create/restore (shipped)                    |
| DocumentDB | **ADD** snapshots + retention + restore window              | `docdb:DescribeDBClusterSnapshots` + `docdb:DescribeDBClusters` | create-snapshot/restore → backlog           |
| DynamoDB   | **ADD** PITR status + restorable window + on-demand backups | `dynamodb:DescribeContinuousBackups` + `dynamodb:ListBackups`   | enable-PITR/create-backup/restore → backlog |

## Architecture

The backup **read** lives in `api/dashboard/handler.py:_backups()` (endpoint
`/api/dashboard/{id}/backups`), currently gated `if fam != "relational": return`
(line ~2189). The **write** path is a separate POST Lambda (`api/backups/handler.py`)
— unchanged, stays Aurora-only.

### Backend — `_backups()` (`api/dashboard/handler.py`)

- Replace the relational-only early return with per-family branches (mirror the
  existing `_registry_engine` + region/spoke-role resolution used by `_topology`/
  the current `_backups`):
  - **documentdb**: `docdb` client → `describe_db_cluster_snapshots(DBClusterIdentifier=cid)`
    (manual + automated) + `describe_db_clusters(DBClusterIdentifier=cid)` for
    `BackupRetentionPeriod` / `PreferredBackupWindow` / `EarliestRestorableTime` /
    `LatestRestorableTime`. DocDB snapshot shape mirrors RDS — reuse the same
    snapshot serialization as the relational branch.
  - **dynamodb**: `dynamodb` client → `describe_continuous_backups(TableName=name)`
    (PITR status + `EarliestRestorableDateTime`/`LatestRestorableDateTime`) +
    `list_backups(TableName=name)` (on-demand backups). `name` = the table's
    `resource_name` from the registry (NOT the `ddb-<hex>` slug).
- Return a **normalized, engine-tagged** shape so the panel branches cleanly:
  ```
  { cluster_id, engine_family,
    snapshots: [{ id, type, status, created, size_gb? }],   # relational + docdb
    backup_retention_days, preferred_backup_window,          # relational + docdb
    earliest_restorable, latest_restorable,                  # all (docdb/ddb PITR)
    pitr_enabled,                                            # dynamodb
    on_demand_backups: [{ name, status, created, size_bytes }] # dynamodb
  }
  ```
  Relational keeps its current fields (back-compat); new keys are additive/optional.
- **Friendly fallback** on any boto error (same contract as today's `_backups`):
  never leak the raw boto3 error; return an empty "couldn't read" shape.
- Resolve account/region/spoke-role via the existing registry row helper. Demo
  clusters are local-account; cross-account spoke support inherits the existing
  `_session_for`-style pattern if present, else local session.

### IAM (CDK)

Add to the **dashboard Lambda's execution role** (and the cross-account spoke role
policy if/where defined): `docdb:DescribeDBClusterSnapshots`, `docdb:DescribeDBClusters`,
`dynamodb:DescribeContinuousBackups`, `dynamodb:ListBackups`. Locate the dashboard
Lambda in `cdk/stacks/agent_stack.py` (per project structure, dashboard routes are
in the agent stack). CDK-only — never touch IAM directly.

### Frontend — `backup-panel.tsx` + `dashboard/page.tsx`

- Render `<BackupPanel>` for documentdb + dynamodb (currently relational-gated at
  `page.tsx:613`). Pass `engine`.
- **READ-ONLY for non-relational**: hide the create-snapshot input/button and the
  restore controls when `engineFamily(engine) !== "relational"` (those POST to the
  Aurora-only write handler). Show only the read views.
- Engine-appropriate rendering (branch on `engine_family` in the response):
  - **documentdb**: snapshot table (id/type/status/created) + retention-days +
    backup window + restore window (earliest→latest) — same layout as Aurora.
  - **dynamodb**: PITR status badge (enabled/disabled) + restorable window
    (earliest→latest, when enabled) + on-demand backup list (name/status/created/size).
    A clear empty state when PITR is off and no on-demand backups exist.

### Cedar / approval

None. The read is a REST dashboard endpoint (Cedar governs MCP tools, not the
dashboard API). The write handler is unchanged. No approval surface added.

## Testing

- **Unit** (`tests/unit/api/`): `_backups` documentdb branch (mock `docdb` client →
  snapshots + cluster meta) returns the normalized shape; dynamodb branch (mock
  `dynamodb` client → continuous-backups + list-backups) returns pitr + on-demand;
  error path returns the friendly fallback, not a raw exception. Relational branch
  unchanged (regression).
- **Live** (deploy agent stack + frontend): DocDB dashboard `dbops-docdb-test` →
  BackupPanel shows the real automated snapshot (`rds:dbops-docdb-test-...`) +
  1-day retention + 15:05-15:35 window + restore window; no create/restore buttons.
  DynamoDB `dbops-ddb-scenario-test` → "PITR: disabled" + empty on-demand list (its
  true posture). Aurora BackupPanel unchanged (create/restore still present).
- Full backend suite green; frontend tsc + eslint + build clean.

## Out of scope (deferred to NoSQL-write/remediation backlog)

- Snapshot **create** / **restore** for DocumentDB; **enable-PITR** / on-demand
  **backup create** / **restore** for DynamoDB — all writes, need Cedar/approval.
- Cross-account backup reads for spoke accounts beyond the existing session pattern.
