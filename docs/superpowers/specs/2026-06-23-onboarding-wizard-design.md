# Onboarding Wizard (Spoke-Account Setup) — Design

**Date:** 2026-06-23
**Status:** approved

## Problem

Adding a member (spoke) AWS account to DBOps requires the spoke admin to hand-
create an IAM role (`dbops-spoke-role`) that trusts the DBOps hub account and
carries the right read (and optionally write) permissions — but DBOps gives no
guidance or template. The cross-account assume (`_session_for` →
`sts.assume_role(.../dbops-spoke-role)`) and the connection test
(`POST /api/clusters/test-connection` = AssumeRole + DescribeDBClusters) and
discovery/registration (`/api/clusters/discover` + `/bulk-register`) all exist,
but the operator has no turnkey way to (a) get the exact spoke-role template and
(b) follow a guided setup→verify→register flow.

## Goal

A guided onboarding wizard: generate the **CloudFormation template** for the
spoke role (hub-account trust + curated least-privilege permissions, read-only
by default with an optional write-remediation set), then verify connectivity
(reusing the existing test-connection), then hand off to the existing
discover/register flow.

Non-goals: a new ping endpoint (the existing `/api/clusters/test-connection`
already does AssumeRole+DescribeDBClusters); rebuilding discovery/registration
(reuse `/api/clusters`); auto-deploying the template into the spoke account
(the spoke admin deploys it — DBOps only generates it); ExternalId enforcement
(requires changing the shared `_session_for` everywhere — a documented follow-up).

## Architecture

One new backend endpoint (template generation) + a wizard UI that stitches the
generated template, the existing test-connection, and the existing
discover/register into a 3-step flow.

### Components

1. **Template-generation API — `api/onboarding/handler.py`** (admin-gated, fail-closed)

   - `GET /api/onboarding/template?region=<r>&remediation=<bool>` → returns the
     spoke-role **CloudFormation template** (YAML string) + metadata.
   - **Trust:** the spoke role trusts the hub account root —
     `Principal: {AWS: "arn:aws:iam::<HUB_ACCOUNT_ID>:root"}` — because
     `_session_for` assumes the spoke role DIRECTLY from the hub-side Lambda's
     own role (no role-chaining), and hub-side Lambdas hold `sts:AssumeRole`.
     `<HUB_ACCOUNT_ID>` is the DBOps deployment account, derived at runtime via
     `boto3.client("sts").get_caller_identity()["Account"]` (no env needed; the
     hub role ARN for display comes from a `HUB_ROLE_ARN` env).
   - **Role name:** fixed `dbops-spoke-role` (the convention `_session_for` +
     `hub_role`'s `arn:aws:iam::*:role/dbops-spoke-role` assume grant rely on).
   - **Permissions (read-only default):** an inline policy with the curated
     cross-account read action set DBOps uses: `rds:Describe*`,
     `rds-data:ExecuteStatement` + `BatchExecuteStatement`,
     `cloudwatch:GetMetricData` + `GetMetricStatistics` + `ListMetrics`,
     `pi:GetResourceMetrics` + `DescribeDimensionKeys` + `ListAvailableResourceMetrics`,
     `logs:FilterLogEvents` + `GetLogEvents` + `DescribeLogStreams` + `DescribeLogGroups`,
     `dynamodb:ListTables` + `DescribeTable` + `DescribeContinuousBackups` + `DescribeTimeToLive`,
     `secretsmanager:GetSecretValue` + `DescribeSecret` (Resource scoped to
     `arn:aws:secretsmanager:*:*:secret:dbops/*`). (The implementer curates the
     exact list against the cross-account call sites; the read actions above are
     the baseline.)
   - **`remediation=true`** appends the approval-gated write actions the
     operations MCP uses cross-account: `rds:ModifyDBCluster` + `ModifyDBInstance`
     - `ModifyDBParameterGroup`/`ModifyDBClusterParameterGroup` + `CreateDBClusterSnapshot`
     - `CreateDBSnapshot` + the maintenance/reboot actions, `dynamodb:UpdateTable`
     - `UpdateContinuousBackups` + `UpdateTimeToLive`. (Implementer curates
       against the operations MCP write tools.)
   - Response: `{"template": "<yaml>", "hub_account_id", "hub_role_arn",
"role_name": "dbops-spoke-role", "remediation": <bool>, "console_url":
"<CloudFormation create-stack quick-create link with the template URL or a
deep link>"}`. (If a quick-create link needs a hosted template URL, omit it
     or provide the create-stack console URL; the copy/download of the YAML is
     the primary path.)

2. **Wizard UI — `frontend/src/app/onboarding/page.tsx`** (admin-only)

   - Mirror the Settings/approval-policies admin-page shell + the `"admin only"`
     → notice pattern; nav entry `adminOnly: true` (hidden from viewers).
   - **Step 1 — Create the spoke role:** show the hub account id + hub role ARN;
     a read-only ↔ include-remediation toggle (re-fetches the template); the
     CloudFormation YAML in a copy/download block + a "deploy in your member
     account" instruction (+ console link if available). Korean copy.
   - **Step 2 — Verify:** account_id + region inputs → call the EXISTING
     `POST /api/clusters/test-connection` with `{account_id, region, role_arn:
"arn:aws:iam::<account_id>:role/dbops-spoke-role"}` (match its actual
     request contract — read the handler) → show success (green + the assumed
     identity / discovered cluster count) or a diagnostic error (red).
   - **Step 3 — Register:** on a green verify, link/CTA to the existing
     `/clusters` discovery+registration (reuse — do not rebuild).
   - `api-client.ts`: `fetchOnboardingTemplate({region, remediation})`; reuse
     the existing cluster test-connection client fn (add one if absent).

3. **Routes + openapi + nav.** `GET /api/onboarding/template` route in
   agent_stack; regenerate `frontend/public/openapi.json`; nav + ⌘K entries
   (`adminOnly: true`).

## Data Flow

- **Template:** admin → wizard step 1 → `GET /api/onboarding/template?region&remediation`
  → handler derives hub account id (sts caller identity) → emits CloudFormation
  YAML (trust hub root + permission set) → UI copy/download.
- **Verify:** wizard step 2 → existing `POST /api/clusters/test-connection`
  (AssumeRole `dbops-spoke-role` + DescribeDBClusters) → ok/diagnostic.
- **Register:** wizard step 3 → existing `/clusters` discover + bulk-register.

## Error Handling

- Template gen is pure (input validation: region format, remediation bool);
  never calls into the spoke account. `get_caller_identity` failure → 500 with a
  clear message (shouldn't happen — the Lambda always has an identity).
- Verify reuses test-connection's existing structured error handling (AccessDenied
  = trust/role missing, etc.) — no new error surface.
- Admin gate: non-admin → 403 on the template endpoint (fail-closed).

## Testing

- **API:** `GET /api/onboarding/template` → valid CloudFormation YAML containing
  the `dbops-spoke-role` name, the hub-account-root trust principal (with the
  account id from a mocked `get_caller_identity`), and the read action set;
  `remediation=true` adds the write actions, `false`/absent does not; admin gate
  (viewer/no-bearer → 403); openapi parity.
- **UI:** `npm run build`.

## Security

- The template endpoint is admin-gated + fail-closed; it emits an IAM template
  (no secrets) and reads only the hub's own account id.
- The generated role is **read-only by default**; write-remediation is an
  explicit opt-in toggle. The trust is hub-account-root (matches the existing
  direct-assume model); **ExternalId hardening is a documented follow-up** — it
  requires threading an ExternalId through the shared `_session_for` used by
  clusters discover/test-connection too, so adding it only to a new spoke role
  would break the existing cross-account path. Tracked separately.
- DBOps never deploys into the spoke account; the spoke admin reviews + deploys
  the template themselves (operator-controlled, least-privilege).
