# Rich HTML Reports (inline SVG charts) — Design

**Date:** 2026-06-24
**Status:** approved (backlog "리포트 리치 포맷"; approach chosen with Codex — self-contained HTML + pure-Python inline SVG, no heavy bundle, no external chart service)

## Context

Report delivery is done (opt-in SNS/Slack/Teams push + a `/reports` web viewer that
renders the JSON `data` + Bedrock summary). The remaining gap was a **downloadable /
attachable rich report with charts**. Per the Codex consult, the chosen approach is
**a self-contained HTML report with inline pure-Python SVG charts** — near-zero
Lambda bundle increase, no external data transfer (privacy), one HTML works for S3
storage + `/reports` download + email/Slack/Teams attachment. (matplotlib/Chromium
rejected — heavy; client-side html2canvas rejected — can't attach to push delivery.)

`report_generator` already builds a per-cluster `data` dict (AAS avg/max + AAS
time-series + busy minutes + top slow queries + alert-rule counts + findings) and a
Bedrock NL summary, stores them to `reports/{cid}/{date}-{type}.json` in S3 + a
`reports` DB row, and delivers the digest. This adds an HTML twin.

## Architecture

### Component 1 — `data-pipeline/report_generator/report_html.py` (new, pure compute)

Pure-Python, no third-party deps (no bundle change). Builds inline-SVG charts +
assembles a self-contained HTML document. Engine-agnostic: renders whatever the
`data` dict contains (a report for any engine family).

- **SVG chart primitives** (each returns an SVG `<svg>…</svg>` string, fixed
  viewBox, no external refs):
  - `line_chart(points, ...)` — a time-series line (e.g. AAS over the window) with
    axis ticks; tolerates empty/short series (renders an "데이터 없음" placeholder).
  - `bar_chart(rows, ...)` — horizontal bars for a labeled count list (top slow
    queries, alert-rule counts, findings-by-severity).
  - `sparkline(values)` — a compact inline trend (for headline metric cards).
  - `severity_badge(counts)` — colored critical/warning/info pills.
- **`build_report_html(cluster_id, report_date, report_type, summary, data) -> str`**
  — a complete `<!doctype html>` document: header (cluster, date, type), the NL
  summary (escaped), headline stat cards (with sparklines), the line chart for the
  primary time-series, bar charts for top-N lists, the findings table + severity
  badges. All CSS inlined in a `<style>` block; all charts inline SVG; **no external
  fonts/scripts/images** (self-contained, safe to attach/open offline). All
  user/DB-derived text HTML-escaped (the summary + query excerpts + finding text)
  to prevent HTML injection in the report.

### Component 2 — `report_generator/handler.py` wiring

After the existing JSON `put_object` + the `reports` INSERT, ALSO:

- `html = build_report_html(cid, report_date, report_type, summary_text, report_data)`
- `put_object(Bucket=s3_bucket, Key=html_key, Body=html, ContentType="text/html; charset=utf-8")`
  where `html_key = s3_key[:-5] + ".html"` (the JSON key with `.json`→`.html`) — **no
  schema change** (the HTML key is derivable from the stored `s3_key`).
- Wrapped in try/except (best-effort, mirrors the JSON put) — an HTML-render failure
  must NOT break report generation or delivery.

(Delivery: `_deliver_report` continues to push the short text summary + link; a
follow-up could attach the HTML where the channel supports it. Not in this spec —
attaching to SNS/Slack/Teams varies per channel; the download path is the deliverable.)

### Component 3 — `/reports` download (api/reports + frontend)

- **`api/reports/handler.py`**: add a `GET /api/reports/{id}/html` arm that returns a
  presigned S3 URL (or the HTML body) for the report's `.html` key — derive the html
  key from the row's `s3_key` (`.json`→`.html`), `head_object` to confirm it exists
  (older reports predate the HTML twin → 404 with a clear note), else presigned GET
  (short TTL) or passthrough. Read-only.
- **Frontend `/reports`**: add an "HTML 다운로드" button on the report viewer that
  opens the presigned URL / downloads the HTML. Hidden/disabled when the report has
  no HTML twin (pre-existing reports).

## Data Flow

ETL cadence → `report_generator` builds `data` + summary → stores JSON + **HTML**
(inline SVG) to S3 + the `reports` row → `/reports` lists; the viewer offers an
"HTML 다운로드" that fetches the presigned `.html`. Self-contained HTML opens
offline / attaches to email.

## Error Handling

- HTML render/put wrapped in try/except (best-effort; JSON + DB row + delivery
  unaffected on failure).
- SVG builders tolerate empty/missing series (placeholder, no crash).
- Download arm: missing `.html` (old report) → 404 + note ("이 리포트는 HTML 생성 이전에 만들어졌습니다").
- All report text HTML-escaped (injection-safe).

## Testing

- **`report_html` unit** (`tests/unit/data_pipeline/test_report_html.py`):
  `build_report_html` returns a non-empty `<!doctype html>` containing the cluster
  id, the (escaped) summary, and `<svg` for each chart; empty `data` → still valid
  HTML (placeholders, no crash); a summary/query-excerpt with `<script>`/`&`/`<` is
  HTML-escaped (no raw injection); the line/bar builders handle empty input.
- **handler unit**: the handler puts BOTH a `.json` and a `.html` object (mock S3);
  an HTML-render exception does not stop the JSON put / DB insert / delivery.
- **api/reports unit**: the `/{id}/html` arm derives the `.html` key + returns a
  presigned URL when present, 404 when absent.
- **Frontend**: `npm run build` clean; the download button renders (disabled when no
  HTML twin).
- Full unit suite + CDK synth green (no new bundle dep, no schema migration; confirm
  the report_generator + api/reports Lambdas already have the needed S3 read/write).

## Security

- No external chart service / fonts / scripts — fully self-contained HTML (customer
  metrics never leave AWS).
- All DB/AI-derived text HTML-escaped → no stored-HTML injection in the report.
- Download via short-TTL presigned GET (read-only) on the report's own S3 key; the
  api/reports auth gate is unchanged.
- No new heavy dependency, no schema migration, no new IAM beyond S3 get/put the
  report Lambdas already hold for the archive bucket (confirm; add only if absent).
