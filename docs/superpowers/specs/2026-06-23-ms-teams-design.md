# Microsoft Teams Alert/Report Delivery — Design

**Date:** 2026-06-23
**Status:** approved

## Problem

DBOps delivers alerts (alert_evaluator) and report digests (report_generator)
to managed subscribers over `slack-webhook` and `pagerduty-events-v2`, but not
Microsoft Teams. Teams shops can't receive DBOps notifications.

## Goal

Add a `teams-webhook` managed subscriber protocol that mirrors the existing
Slack delivery: alerts and report digests POST a Teams MessageCard to the
subscriber's Teams Incoming Webhook URL. Opt-in (admins add a subscriber); no
new infra; best-effort delivery (existing failure-recording pattern).

Non-goals: Teams bot / two-way interaction; Adaptive Cards via Power Automate
Workflows (MessageCard via classic Incoming Webhook is the simplest working
path — see Migration note); changing the Slack/PagerDuty paths.

## Architecture

A third protocol branch in the two delivery paths (alert_evaluator,
report_generator), a Teams MessageCard builder per path, the protocol added to
the subscriber allowlist + validated, and a Teams option in the alerts UI.

### Components

1. **Alert delivery — `data-pipeline/alert_evaluator/handler.py`**

   - `_build_teams_payload(rule, latest) -> dict` — a Teams **MessageCard**:
     `{"@type":"MessageCard","@context":"http://schema.org/extensions","summary",
"themeColor": <severity hex>, "title": "🚨 DBOps alert · {cluster_id}",
"sections":[{"facts":[Rule/Metric/Threshold/Observed], "markdown": true}],
"potentialAction":[OpenUri buttons (timeline/dashboard/alerts) when
FRONTEND_URL is set]}`. Mirrors `_build_slack_payload`'s content +
     deep-links (reuse the existing `_dashboard_url` helper).
   - Delivery loop: add `elif protocol == "teams-webhook": payload =
_build_teams_payload(rule, latest); url = endpoint`. Reuses the existing
     `_post_json` + `last_used_at`/`last_error` recording.

2. **Report delivery — `data-pipeline/report_generator/handler.py`**

   - `_build_report_teams_card(cluster_id, report_date, report_type, summary) -> dict`
     — a MessageCard with the digest summary (mirrors `_build_report_slack_blocks`).
   - `_deliver_report` currently queries `protocol = 'slack-webhook'` subscribers.
     Broaden to also fetch `teams-webhook` subscribers and POST the Teams card to
     each (same best-effort try/except per subscriber). Keep `REPORT_DELIVERY_ENABLED`
     gating unchanged.

3. **Subscriber registration — `api/alerts/handler.py`**

   - Add `"teams-webhook"` to `_MANAGED_PROTOCOLS` (the add/list/delete managed
     paths are generic). In `_create_subscription`, validate the teams endpoint:
     `if protocol == "teams-webhook" and not endpoint.startswith("https://"):
return 400` (Teams Incoming Webhook hosts vary — `*.webhook.office.com`,
     `*.logic.azure.com` — so require https + non-empty, lenient like Slack but
     not host-locked).

4. **UI — `frontend/src/app/alerts/page.tsx`** (the alerts subscriber form)
   - Add a "Microsoft Teams" option to the add-subscriber protocol selector
     (value `teams-webhook`), with a Korean hint + a placeholder for the Teams
     Incoming Webhook URL. Mirror the existing Slack option's UX. If the form's
     protocol options are a typed union/list, extend it.

## Data Flow

- **Alert:** rule fires → alert_evaluator reads enabled subscribers → for a
  `teams-webhook` row, build the MessageCard → `_post_json(endpoint, card)` →
  record last_used_at/last_error.
- **Report:** report_generator (when REPORT_DELIVERY_ENABLED) → fetch
  teams-webhook subscribers → POST the report MessageCard to each.
- **Register:** admin → alerts UI → `POST /api/alerts` `{protocol:"teams-webhook",
endpoint:<webhook url>, label}` → validated → `alert_subscribers_managed` row.

## Error Handling

- Delivery is best-effort (existing pattern): a non-2xx or exception is recorded
  to `last_error` and the loop continues — never aborts alert/report processing.
- Subscriber add: non-https teams endpoint → `400`; empty endpoint → `400`.

## Testing

- **alert_evaluator:** `_build_teams_payload` produces a valid MessageCard
  (`@type` == "MessageCard", themeColor by severity, the four facts present,
  OpenUri actions when FRONTEND_URL set / absent when not); the `teams-webhook`
  branch posts to the endpoint (mock `_post_json`, assert URL == endpoint).
- **report_generator:** a `teams-webhook` subscriber receives the report card
  (mirror the slack-delivery test; mock the subscriber query + `_post_json`).
- **api/alerts:** `teams-webhook` add with https → 201; non-https → 400; listed
  among managed subscribers.
- **UI:** `npm run build`.

## Security

- Same surface as the existing Slack/PD subscriber management (admin-gated where
  the alerts API already gates writes); the endpoint is an outbound webhook URL
  the operator supplies. No inbound surface, no secrets stored beyond the
  operator-provided webhook URL (same as Slack today).

## Migration note

Microsoft is deprecating O365 connectors (MessageCard via classic Incoming
Webhooks) in favor of Power Automate Workflows + Adaptive Cards. MessageCard
still works with existing Incoming Webhooks and is the simplest path today; a
future increment can add an Adaptive-Card/Workflows variant (different envelope)
behind the same `teams-webhook` protocol if/when classic webhooks are retired.
