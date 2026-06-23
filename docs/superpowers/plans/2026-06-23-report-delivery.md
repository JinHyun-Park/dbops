# Report Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Deliver scheduled report digests to managed Slack subscribers + the SNS topic (email), opt-in, reusing the alert delivery infra; add a client-side report download on /reports.

**Architecture:** `report_generator` (data stack, daily) gains an opt-in `_deliver_report` step that publishes the NL summary to the SNS topic and POSTs a Block Kit digest to enabled `slack-webhook` subscribers — mirroring `alert_evaluator`'s subscriber-delivery (copied per the repo's per-Lambda-copy convention). The `/reports` page adds a client-side markdown download of the already-fetched report (no backend change).

**Tech Stack:** Python 3.12 Lambda (RDS Data API via the handler's `cache_query`, boto3 SNS, urllib POST), AWS CDK (Python), Next.js 16 static export, pytest.

## Global Constraints

- CDK-only infrastructure (AGENTS.md).
- Delivery is **opt-in and inert by default**: `REPORT_DELIVERY_ENABLED` unset/false → `_deliver_report` is a no-op; existing report generation/storage is byte-identical. Existing alert subscribers must NOT start receiving reports unless the flag is on.
- Delivery failures are isolated: any SNS/Slack failure is logged and never aborts `lambda_handler` or the per-cluster loop.
- Reuse existing infra: `alert_subscribers_managed` table (cols `id, protocol, endpoint, enabled`), `ALERT_TOPIC_ARN` SNS topic, `self.alert_topic.grant_publish`. NO SES. PagerDuty is excluded (reports → slack-webhook + SNS only).
- RDS Data API SQL via the handler's existing `cache_query`; named params; agent/audit SQL comment convention already handled by the helper.
- Korean human-facing text (Slack digest title/labels, download content headings); DB jargon English.
- Commits: conventional subject; NO `Co-Authored-By: Claude` trailer; no internal-roadmap refs. Frontend commit hits the prettier hook — `git add -A` + re-commit if it reformats.
- Adding NO API route in this feature → no openapi regen needed (download is client-side; delivery is backend-only with no new route).

---

## File Structure

**Increment 1 — Delivery (data stack)**

- Modify: `data-pipeline/report_generator/handler.py` — `_post_json`, `_build_report_slack_blocks`, `_deliver_report`; call from `lambda_handler` gated on `REPORT_DELIVERY_ENABLED`.
- Modify: `cdk/stacks/data_stack.py` — report_generator env (`ALERT_TOPIC_ARN`, `REPORT_DELIVERY_ENABLED`) + `grant_publish`.
- Modify: `cdk/config/settings.example.py` — documented `REPORT_DELIVERY_ENABLED = False`.
- Test: `tests/unit/data_pipeline/test_report_delivery.py` (new).

**Increment 2 — Download (frontend only)**

- Modify: `frontend/src/app/reports/page.tsx` — client-side markdown download button.
- (Optional) Create: `frontend/src/lib/report-download.ts` — pure markdown-assembly helper (unit-testable).

---

## Increment 1 — Delivery

### Task 1: report_generator delivers digest (SNS + managed Slack), opt-in

**Files:**

- Modify: `data-pipeline/report_generator/handler.py`
- Modify: `cdk/stacks/data_stack.py`
- Modify: `cdk/config/settings.example.py`
- Test: `tests/unit/data_pipeline/test_report_delivery.py`

**Interfaces:**

- Produces: nothing consumed by later tasks; behavioural — reports get delivered when `REPORT_DELIVERY_ENABLED` is truthy.

- [ ] **Step 1: Write failing tests** — `tests/unit/data_pipeline/test_report_delivery.py`:

Read `report_generator/handler.py` first to match the real `lambda_handler` signature/flow and how `cache_query` + the report loop work; mirror the existing test style in `tests/unit/data_pipeline/`. The tests target the new `_deliver_report` directly (unit) — flag/subscriber/exception behaviour:

```python
import importlib
from unittest.mock import MagicMock, patch

h = importlib.import_module("report_generator.handler")  # adjust import to match repo test convention


def test_deliver_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("REPORT_DELIVERY_ENABLED", raising=False)
    cache_query = MagicMock()
    with patch.object(h, "boto3") as mboto:
        h._deliver_report(cache_query, "c1", "2026-06-23", "daily", "요약")
    mboto.client.assert_not_called()   # no SNS
    cache_query.assert_not_called()    # no subscriber read


def test_deliver_sns_when_enabled_no_slack_subs(monkeypatch):
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:::t")
    cache_query = MagicMock(return_value=[])  # no managed slack subs
    sns = MagicMock()
    with patch.object(h, "boto3") as mboto, patch.object(h, "_post_json") as mpost:
        mboto.client.return_value = sns
        h._deliver_report(cache_query, "c1", "2026-06-23", "daily", "요약")
    sns.publish.assert_called_once()
    mpost.assert_not_called()          # no slack subscribers → no POST


def test_deliver_posts_to_slack_subs(monkeypatch):
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:::t")
    cache_query = MagicMock(return_value=[{"id": 1, "protocol": "slack-webhook", "endpoint": "https://hooks.slack/x"}])
    with patch.object(h, "boto3"), patch.object(h, "_post_json", return_value=(200, "ok")) as mpost:
        h._deliver_report(cache_query, "c1", "2026-06-23", "daily", "요약")
    mpost.assert_called_once()


def test_delivery_exception_is_swallowed(monkeypatch):
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")
    cache_query = MagicMock(side_effect=RuntimeError("db down"))
    with patch.object(h, "boto3"):
        h._deliver_report(cache_query, "c1", "2026-06-23", "daily", "요약")  # must not raise
```

- [ ] **Step 2: Run, verify fail** — `python3 -m pytest tests/unit/data_pipeline/test_report_delivery.py -q` (no `_deliver_report`).

- [ ] **Step 3: Implement** in `report_generator/handler.py`:

1. Copy `_post_json` from `data-pipeline/alert_evaluator/handler.py` (the urllib JSON POST returning `(status, body)`).
2. Add a Block Kit builder + the deliver function:

```python
import os

def _build_report_slack_blocks(cluster_id, report_date, report_type, summary):
    return {
        "text": f"DBOps 리포트 · {cluster_id}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"📋 DBOps 리포트 · {report_date}"}},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*클러스터* `{cluster_id}` · *유형* {report_type}\n\n{summary[:2800]}"}},
        ],
    }

def _deliver_report(cache_query, cluster_id, report_date, report_type, summary):
    """Best-effort: publish the digest to SNS (email) + POST to managed slack-webhook
    subscribers. Inert unless REPORT_DELIVERY_ENABLED is truthy. Never raises."""
    if os.environ.get("REPORT_DELIVERY_ENABLED", "").strip().lower() not in ("true", "1", "yes", "on"):
        return
    try:
        topic = os.environ.get("ALERT_TOPIC_ARN", "")
        if topic:
            boto3.client("sns").publish(
                TopicArn=topic,
                Subject=f"DBOps 리포트 · {cluster_id} · {report_date}"[:100],
                Message=summary,
            )
        subs = cache_query(
            "SELECT id, protocol, endpoint FROM alert_subscribers_managed "
            "WHERE enabled = true AND protocol = 'slack-webhook'"
        )
        for s in subs or []:
            try:
                _post_json(s["endpoint"], _build_report_slack_blocks(cluster_id, report_date, report_type, summary))
            except Exception as e:
                print(f"[report-gen] slack deliver failed for sub {s.get('id')}: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[report-gen] delivery failed for {cluster_id}: {type(e).__name__}: {e}")
```

3. In `lambda_handler`, after the report is stored + `summary` computed (the `reports` INSERT around lines 100–108), call `_deliver_report(cache_query, cid, report_date, report_type, summary)`. Use the variables already in scope; place it inside the per-cluster loop after storage, wrapped so a delivery issue can't skip remaining clusters (the function already swallows, but keep it after the INSERT).

- [ ] **Step 4: CDK** in `cdk/stacks/data_stack.py` — add to the `report_generator` `environment` dict:

```python
                "ALERT_TOPIC_ARN": self.alert_topic.topic_arn,
                "REPORT_DELIVERY_ENABLED": "true" if getattr(Settings, "REPORT_DELIVERY_ENABLED", False) else "false",
```

and after the existing grants:

```python
        self.alert_topic.grant_publish(self.report_generator)
```

(`report_generator` already has cache read via `grant_data_api_access` + secret read, so the subscriber query needs no new grant.)

- [ ] **Step 5: settings.example** — add to `cdk/config/settings.example.py` (near other notification settings):

```python
    # Push generated report digests to managed Slack subscribers + the SNS
    # topic (email). False (default) keeps report delivery off so existing
    # alert subscribers don't start receiving daily reports. Turn on once you
    # want scheduled reports delivered, not just viewable in /reports.
    REPORT_DELIVERY_ENABLED = False
```

- [ ] **Step 6: Run tests + synth** — `python3 -m pytest tests/unit/data_pipeline/test_report_delivery.py tests/unit/data_pipeline -q` (new pass + no regression); `cd cdk && cdk synth dbops-dev-data --quiet` exit 0.

- [ ] **Step 7: Commit** — add `data-pipeline/report_generator/handler.py cdk/stacks/data_stack.py cdk/config/settings.example.py tests/unit/data_pipeline/test_report_delivery.py` ; `git commit -m "feat(reports): deliver report digests to Slack subscribers + SNS (opt-in)"`

---

## Increment 2 — Download (frontend only)

### Task 2: client-side markdown download on /reports

**Files:**

- Modify: `frontend/src/app/reports/page.tsx`
- (Optional) Create: `frontend/src/lib/report-download.ts`

**Interfaces:**

- Consumes: the report object already fetched for the selected row (`summary`, `data`, `cluster_id`, `report_date`/equivalent fields — read the actual `Report` type in the page).

- [ ] **Step 1: Read the page** — `frontend/src/app/reports/page.tsx`: the `Report`/row type (fields: `summary`, `data`, `s3_key`, `cluster_id`, date), how the selected report detail is fetched (`/api/reports/${id}`) and where its summary/data are rendered. Identify where to place a "다운로드" button (near the detail view).

- [ ] **Step 2: Add the markdown assembler + download** — build a markdown string from the fetched report: a title (`# DBOps 리포트 — {cluster_id} ({date})`), the summary, then the key `data` entries as a list/table. Then:

```typescript
const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
const url = URL.createObjectURL(blob);
const a = document.createElement("a");
a.href = url;
a.download = `report-${clusterId}-${date}.md`;
a.click();
URL.revokeObjectURL(url);
```

Render the "다운로드" button only when the report detail (with `data`/`summary`) is loaded; disable/hide otherwise. Reuse the page's existing button styling. If you extract the assembler to `lib/report-download.ts` as a pure function `buildReportMarkdown(report): string`, add a tiny unit test if the repo has frontend unit tests (else skip — build is the gate).

- [ ] **Step 3: Build** — `cd frontend && npm run build` → exit 0, `/reports` in the route list.

- [ ] **Step 4: Commit (mind prettier)** — `git add frontend/src/app/reports/page.tsx` (+ lib helper if created) ; `git commit -m "feat(reports): client-side markdown download of a report"` (if prettier reformats: `git add -A` + re-run).

---

## Self-Review

- Spec §2.1 (delivery: SNS + managed Slack, opt-in, isolated) → Task 1. ✓
- Spec §3.1 (report_generator \_deliver_report + CDK env/grant + settings) → Task 1. ✓
- Spec §2.2/§3.3 (client-side download) → Task 2. ✓
- Spec §6 (tests) → Task 1 unit (flag off/on/subs/exception), Task 2 build + optional pure-fn test. ✓
- Non-breaking: `_deliver_report` returns immediately when flag off (byte-identical generation); delivery exceptions swallowed; download is additive frontend-only. ✓
- Type/name consistency: `alert_subscribers_managed` cols (`id,protocol,endpoint,enabled`) match the alert_evaluator query; `ALERT_TOPIC_ARN`/`grant_publish` match the data_stack convention; `_post_json` copied verbatim from alert_evaluator. ✓
