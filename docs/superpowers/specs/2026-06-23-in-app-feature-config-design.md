# In-App Feature Configuration — Design

**Date:** 2026-06-23
**Status:** approved

## Problem

DBOps ships several **opt-in** capabilities that are currently gated by Lambda
environment variables baked at deploy time:

- `TICKETING_PROVIDER` (default `"none"`) — read in
  `mcp-servers/mcp_servers/workers/ticketing.py::get_provider`.
- `REPORT_DELIVERY_ENABLED` (default `"false"`) — read in
  `data-pipeline/report_generator/handler.py::_deliver_report`.

To turn either of these on, someone who deployed DBOps from this repo must edit
`cdk/config/settings.py` (or a Lambda env) and **redeploy**. That is an adoption
barrier: a team that wants to enable the ticketing seam or report delivery has
to touch infrastructure code. The seams we already built are inert until a
redeploy flips an env var.

## Goal

Let a DBOps **admin** toggle these feature settings **from inside the DBOps web
UI**, persisted to a database, with **no code change and no redeploy**. The
existing env-var defaults remain the fallback, so a fresh deploy behaves exactly
as today until an admin changes something.

Non-goals (deferred to the Admin-console backlog item): user/role management,
multi-team page sharing, arbitrary free-form config keys.

## Architecture

A small **key-value config store** in DynamoDB, an **admin-gated REST API** to
read and write it, and a **read path** wired into the two consumer Lambdas with
an env-var fallback and a short in-process cache. A settings page in the web UI
drives the API.

Precedence at read time: **stored DB value → env var → built-in default.**
This keeps every existing deploy unchanged (no rows ⇒ env/default wins) while
letting an admin override without redeploying.

### Components

1. **`dbops-{env}-app-config` DynamoDB table** (FoundationStack)

   - Partition key `config_key` (S). Attributes: `value` (S — every value is
     stored as a string), `updated_at` (S, ISO-8601 UTC), `updated_by` (S).
   - One item per config key. `PAY_PER_REQUEST`, PITR on, `DESTROY` removal
     policy (matches the other foundation tables).
   - Lives in **foundation** so both the agent stack (config API + task worker)
     and the data stack (report generator) can reference it without a
     cross-stack cycle — same rationale as `agent_tasks_table`.
   - Two grant helpers on `FoundationStack`, mirroring `grant_task_manage`:
     - `grant_app_config_read(fn)` → sets `APP_CONFIG_TABLE` env + read grant.
     - `grant_app_config_write(fn)` → sets `APP_CONFIG_TABLE` env + read/write grant.

2. **Config REST API** — `api/config/handler.py`, new Lambda `ConfigApi` in
   AgentStack, routes `GET /api/config` and `PUT /api/config`.

   - Both methods are **admin-gated** (`_is_admin`, same helper/dev-fallback as
     the other handlers). A `dbops-viewer` gets `403`.
   - **Known-keys allowlist** lives server-side. v1 keys:
     - `TICKETING_PROVIDER` — string, default `"none"`. Validation: format only
       (`^[a-z0-9_-]{1,32}$`, lower-cased). The API does **not** check provider
       membership — `get_provider` already returns an inert `_UnwiredProvider`
       for an unknown name, so an unwired value is safe. This keeps the API
       decoupled from the `_IMPLEMENTED` registry in mcp-servers.
     - `REPORT_DELIVERY_ENABLED` — boolean, default `"false"`. Validation:
       accept JSON `true`/`false` or the strings `true/false/1/0/yes/no/on/off`;
       normalize to `"true"`/`"false"`.
   - `GET /api/config` → `{"items": [{"key", "value", "default", "updated_at",
"updated_by"}]}` — one entry per allowlisted key, in allowlist order;
     `value` is the stored value or the key's default; `updated_at`/`updated_by`
     are `null` when unset.
   - `PUT /api/config` body `{"config": {KEY: value, ...}}` → validate every key
     (unknown key or bad value ⇒ `400`, nothing written), upsert each provided
     key with `updated_at`/`updated_by` (from the caller's JWT), return the same
     shape as `GET`. Keys omitted from the body are left unchanged.
   - OpenAPI: regenerate `frontend/public/openapi.json` via
     `python tools/openapi_gen.py`; `tests/unit/test_openapi_spec.py` enforces parity.

3. **Consumer read path** — a `get_config(key, default)` helper that reads the
   config table by key with a **~60s module-level TTL cache**, falling back to
   the env var of the same name, then the passed default.

   - mcp-servers: `mcp_servers/shared/app_config.py::get_config`. `ticketing.get_provider`
     resolves a `None` name through it: `get_config("TICKETING_PROVIDER",
os.environ.get("TICKETING_PROVIDER", "none"))`.
   - report_generator: a local copy (`data-pipeline/report_generator/app_config.py`)
     — the data-pipeline package shares no layer with mcp-servers, so a small
     per-Lambda copy follows the existing convention. `_deliver_report` gates on
     `get_config("REPORT_DELIVERY_ENABLED", os.environ.get("REPORT_DELIVERY_ENABLED", "false"))`.
   - Wiring: `foundation.grant_app_config_read(task_worker)` (AgentStack) and
     `foundation.grant_app_config_read(self.report_generator)` (DataStack).
   - Failure mode: any DDB error in `get_config` is swallowed and the env/default
     fallback is used — config reads must never break task completion or report
     generation (same fail-safe posture as the seams they gate).

4. **Settings UI** — `frontend/src/app/settings/page.tsx`, admin-only, mirroring
   the `/preferences` page shell. A toggle for report delivery and a text/select
   for the ticketing provider, loaded from `GET /api/config`, saved via
   `PUT /api/config`. Non-admins (or a `403`) see an "admins only" notice. New
   `fetchAppConfig` / `updateAppConfig` in `api-client.ts`; a nav entry.

## Data Flow

- **Read (admin opens settings):** browser → `GET /api/config` → ConfigApi
  Lambda → DDB scan/batch-get of allowlisted keys → merged with defaults → UI.
- **Write (admin toggles):** browser → `PUT /api/config` → validate → DDB
  `put_item` per key → returns merged config → UI updates.
- **Consume (feature runtime):** task_worker / report_generator call
  `get_config(...)` → DDB get_item (cached ≤60s) → env fallback → default. A
  freshly-changed setting takes effect within the cache TTL on warm containers,
  immediately on cold.

## Error Handling

- API: unknown key or invalid value → `400` with a message, no partial write.
  Non-admin → `403`. Malformed JSON body → `400`.
- Consumers: `get_config` never raises — DDB/permission errors fall back to env
  then default, logged at most once per failure.

## Testing

- **Increment 1:** CDK snapshot test updates; assert the table + grant helpers
  synthesize.
- **Increment 2:** handler unit tests — GET returns defaults when empty, admin
  gate (403 for viewer), PUT validation (unknown key, bad provider format, bool
  normalization), PUT persists + echoes; `test_openapi_spec.py` parity passes
  with the new route.
- **Increment 3:** `get_config` precedence tests (DB > env > default), cache TTL
  behavior, DDB-error fallback; `ticketing.get_provider` resolves through it.
- **Increment 4:** frontend build (`npm run build`) green; manual/admin live check.

## Security

- Writes are admin-only at the handler (server-side `_is_admin`), not just the
  UI. The dev fallback (no Cognito groups ⇒ admin) matches every existing
  handler and is intentional for local/dev.
- The config store holds only operational toggles, never secrets. Provider
  credentials (Jira/ServiceNow tokens, etc.) remain in Secrets Manager and are
  out of scope here.
