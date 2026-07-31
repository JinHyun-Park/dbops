# Rich HTML Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-contained HTML report (inline pure-Python SVG charts) generated alongside the JSON report, downloadable from `/reports`.

**Architecture:** New `report_html.py` (pure-Python SVG + HTML assembler, no deps) → `report_generator` stores an `.html` twin next to the `.json` → `api/reports` serves a presigned download → a frontend "HTML 다운로드" button.

**Tech Stack:** Python 3.12 (report_generator + api/reports Lambdas), Next.js 16. No new dependency.

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** (user rule).
- **No third-party deps** (no Lambda bundle change): SVG built as strings in pure Python.
- **No external fonts/scripts/images** in the HTML — fully self-contained (privacy + offline + attachable).
- **HTML-escape ALL DB/AI-derived text** (summary, query excerpts, finding text) via `html.escape` — injection-safe.
- **No schema migration:** the HTML S3 key is the JSON key with `.json`→`.html`.
- **Best-effort:** an HTML render/put failure must NOT break the existing JSON put / DB insert / delivery.
- **Korean** copy; metric/engine tokens verbatim. Numbers ≥1000 formatted with thousands separators.

---

### Task 1: `report_html.py` — SVG charts + HTML assembler

**Files:**

- Create: `data-pipeline/report_generator/report_html.py`
- Test: `tests/unit/data_pipeline/test_report_html.py`

**Interfaces:**

- Produces: `build_report_html(cluster_id, report_date, report_type, summary, data) -> str`; helpers `line_chart(points)`, `bar_chart(rows)`, `sparkline(values)`, `severity_badge(counts)`.

- [ ] **Step 1: Write the failing test.** Create `tests/unit/data_pipeline/test_report_html.py`:

```python
import importlib.util
from pathlib import Path

_C = Path(__file__).resolve().parents[3] / "data-pipeline/report_generator/report_html.py"
_spec = importlib.util.spec_from_file_location("report_html", _C)
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

_DATA = {
    "aas_avg": 1.2, "aas_max": 4.5,
    "aas_series": [{"ts": "2026-06-24T00:00:00Z", "value": 1.0},
                   {"ts": "2026-06-24T01:00:00Z", "value": 2.5}],
    "top_queries": [{"query_excerpt": "SELECT * FROM orders", "count": 9},
                    {"query_excerpt": "UPDATE <x> SET y", "count": 3}],
    "findings": [{"severity": "warning", "subject": "X", "recommendation": "do Y"}],
}


def test_build_html_is_self_contained_and_has_charts():
    html = rh.build_report_html("my-cluster", "2026-06-24", "daily", "요약 텍스트", _DATA)
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "my-cluster" in html
    assert "요약 텍스트" in html
    assert html.count("<svg") >= 2          # at least the line + a bar chart
    # self-contained: no external refs
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html.lower()    # no scripts


def test_summary_and_query_text_html_escaped():
    evil = "<script>alert(1)</script> & <b>"
    html = rh.build_report_html("c", "2026-06-24", "daily", evil,
                                {"top_queries": [{"query_excerpt": "<img src=x>", "count": 1}]})
    assert "<script>alert(1)</script>" not in html   # raw injection blocked
    assert "&lt;script&gt;" in html                  # escaped form present
    assert "<img src=x>" not in html


def test_empty_data_still_valid_html_no_crash():
    html = rh.build_report_html("c", "2026-06-24", "daily", "", {})
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "데이터 없음" in html   # placeholder for empty series


def test_chart_builders_tolerate_empty():
    assert "<svg" in rh.line_chart([])
    assert "<svg" in rh.bar_chart([])
```

- [ ] **Step 2: Run it to verify it fails.** `python -m pytest tests/unit/data_pipeline/test_report_html.py -q` → FAIL (module missing).

- [ ] **Step 3: Create `data-pipeline/report_generator/report_html.py`:**

```python
"""Pure-Python self-contained HTML report builder with inline SVG charts.

No third-party deps (no Lambda bundle change), no external fonts/scripts/images
(privacy + offline + attachable). All DB/AI-derived text is HTML-escaped."""

from html import escape

_W, _H, _PAD = 640, 180, 28  # chart viewBox


def _fmt(n):
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{f:,.2f}".rstrip("0").rstrip(".") if f % 1 else f"{int(f):,}"


def _placeholder(msg="데이터 없음"):
    return (f'<svg viewBox="0 0 {_W} {_H}" width="100%" role="img">'
            f'<rect width="{_W}" height="{_H}" fill="#f4f4f5"/>'
            f'<text x="{_W//2}" y="{_H//2}" text-anchor="middle" fill="#71717a" '
            f'font-family="sans-serif" font-size="14">{escape(msg)}</text></svg>')


def line_chart(points, label=""):
    """points: list of {ts, value}. Renders a simple line over the value series."""
    vals = []
    for p in (points or []):
        try:
            vals.append(float(p.get("value")))
        except (TypeError, ValueError):
            pass
    if len(vals) < 2:
        return _placeholder()
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = _PAD + (i / (n - 1)) * (_W - 2 * _PAD)
        y = _H - _PAD - ((v - lo) / rng) * (_H - 2 * _PAD)
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    return (f'<svg viewBox="0 0 {_W} {_H}" width="100%" role="img">'
            f'<rect width="{_W}" height="{_H}" fill="#fff"/>'
            f'<polyline points="{poly}" fill="none" stroke="#0ea5e9" stroke-width="2"/>'
            f'<text x="{_PAD}" y="16" fill="#52525b" font-family="sans-serif" '
            f'font-size="11">{escape(label)} (min {_fmt(lo)} / max {_fmt(hi)})</text></svg>')


def bar_chart(rows, label=""):
    """rows: list of {label/query_excerpt/subject, count/value}. Horizontal bars."""
    norm = []
    for r in (rows or []):
        lbl = r.get("label") or r.get("query_excerpt") or r.get("subject") or ""
        try:
            val = float(r.get("count", r.get("value", 0)) or 0)
        except (TypeError, ValueError):
            val = 0.0
        norm.append((str(lbl), val))
    if not norm:
        return _placeholder()
    mx = max((v for _, v in norm), default=1.0) or 1.0
    rowh = 26
    h = _PAD + rowh * len(norm)
    parts = [f'<svg viewBox="0 0 {_W} {h}" width="100%" role="img">'
             f'<rect width="{_W}" height="{h}" fill="#fff"/>']
    for i, (lbl, v) in enumerate(norm):
        y = _PAD + i * rowh
        w = (v / mx) * (_W - 220)
        parts.append(f'<rect x="200" y="{y}" width="{w:.1f}" height="16" fill="#6366f1"/>')
        parts.append(f'<text x="8" y="{y+13}" fill="#3f3f46" font-family="sans-serif" '
                     f'font-size="11">{escape(lbl[:34])}</text>')
        parts.append(f'<text x="{205+w:.0f}" y="{y+13}" fill="#52525b" '
                     f'font-family="sans-serif" font-size="11">{_fmt(v)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def sparkline(values):
    vals = [float(v) for v in (values or []) if isinstance(v, (int, float))]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = " ".join(f"{(i/(n-1))*80:.1f},{20-((v-lo)/rng)*18:.1f}" for i, v in enumerate(vals))
    return (f'<svg viewBox="0 0 80 20" width="80" height="20">'
            f'<polyline points="{pts}" fill="none" stroke="#0ea5e9" stroke-width="1.5"/></svg>')


def severity_badge(counts):
    counts = counts or {}
    colors = {"critical": "#dc2626", "warning": "#d97706", "info": "#2563eb"}
    out = []
    for sev in ("critical", "warning", "info"):
        c = int(counts.get(sev, 0) or 0)
        out.append(f'<span style="background:{colors[sev]};color:#fff;border-radius:9px;'
                   f'padding:2px 8px;font-size:11px;margin-right:6px">{escape(sev)} {c}</span>')
    return "".join(out)


def build_report_html(cluster_id, report_date, report_type, summary, data):
    data = data or {}
    aas_series = data.get("aas_series") or []
    top_queries = data.get("top_queries") or []
    findings = data.get("findings") or []
    sev_counts = {}
    for f in findings:
        s = (f.get("severity") or "info").lower()
        sev_counts[s] = sev_counts.get(s, 0) + 1

    cards = ""
    for k, lbl in (("aas_avg", "AAS 평균"), ("aas_max", "AAS 최대")):
        if data.get(k) is not None:
            cards += (f'<div class="card"><div class="card-lbl">{escape(lbl)}</div>'
                      f'<div class="card-val">{_fmt(data[k])}</div>'
                      f'{sparkline([p.get("value") for p in aas_series])}</div>')

    findings_rows = "".join(
        f'<tr><td>{escape((f.get("severity") or "").upper())}</td>'
        f'<td>{escape(str(f.get("subject") or ""))}</td>'
        f'<td>{escape(str(f.get("recommendation") or ""))}</td></tr>'
        for f in findings) or '<tr><td colspan="3">발견된 항목 없음</td></tr>'

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DBOps 리포트 — {escape(str(cluster_id))} {escape(str(report_date))}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#18181b;margin:0;padding:24px;background:#fafafa}}
h1{{font-size:20px;margin:0 0 4px}} .meta{{color:#71717a;font-size:13px;margin-bottom:20px}}
.summary{{background:#fff;border:1px solid #e4e4e7;border-radius:8px;padding:16px;white-space:pre-wrap;line-height:1.6;margin-bottom:20px}}
.cards{{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
.card{{background:#fff;border:1px solid #e4e4e7;border-radius:8px;padding:12px 16px;min-width:120px}}
.card-lbl{{color:#71717a;font-size:12px}} .card-val{{font-size:22px;font-weight:600}}
.section{{background:#fff;border:1px solid #e4e4e7;border-radius:8px;padding:16px;margin-bottom:20px}}
.section h2{{font-size:14px;margin:0 0 12px;color:#3f3f46}}
table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #f4f4f5}}
</style></head><body>
<h1>DBOps 운영 리포트</h1>
<div class="meta">{escape(str(cluster_id))} · {escape(str(report_date))} · {escape(str(report_type))}</div>
<div class="cards">{cards or ''}</div>
<div class="summary">{escape(str(summary or ''))}</div>
<div class="section"><h2>활동 추이 (AAS)</h2>{line_chart(aas_series, "AAS")}</div>
<div class="section"><h2>상위 쿼리</h2>{bar_chart(top_queries)}</div>
<div class="section"><h2>진단 ({severity_badge(sev_counts)})</h2>
<table><tr><th>심각도</th><th>대상</th><th>권장</th></tr>{findings_rows}</table></div>
</body></html>"""
```

- [ ] **Step 4: Run tests.** `python -m pytest tests/unit/data_pipeline/test_report_html.py -q` → PASS. `python -m pytest tests/unit -q` → no regression.

- [ ] **Step 5: Commit.**

```bash
git add data-pipeline/report_generator/report_html.py tests/unit/data_pipeline/test_report_html.py
git commit -m "feat(reports): self-contained HTML report builder with inline SVG charts"
```

---

### Task 2: Wire the HTML twin into `report_generator`

**Files:**

- Modify: `data-pipeline/report_generator/handler.py` (build + put the `.html` twin)
- Test: extend `tests/unit/data_pipeline/` report_generator tests (find the existing one; else create `test_report_generator_html.py`)

**Interfaces:**

- Consumes: `build_report_html` (Task 1). Produces: an `.html` S3 object alongside each `.json`.

- [ ] **Step 1: Read** `data-pipeline/report_generator/handler.py` lines ~85-115 (the JSON put_object + the `reports` INSERT). Find an existing report_generator test to mirror its mocking (S3 client + cache_query).

- [ ] **Step 2: Write the failing test.** Assert (mocking boto3 S3 + cache_query): one report cycle calls `put_object` for BOTH `reports/{cid}/{date}-{type}.json` AND `...{type}.html` (ContentType text/html); and that a `build_report_html` that raises does NOT prevent the JSON put / the INSERT / `_deliver_report` (patch `build_report_html` to raise, assert the JSON put + insert still happened). Mirror the existing report_generator test harness.

- [ ] **Step 3: Wire it.** In `handler.py`, after the JSON `put_object` block + before/after the INSERT (best-effort, isolated), add:

```python
        if s3_bucket:
            try:
                from report_html import build_report_html  # local import (lazy)
                html_key = s3_key[:-5] + ".html" if s3_key.endswith(".json") else s3_key + ".html"
                boto3.client("s3").put_object(
                    Bucket=s3_bucket, Key=html_key,
                    Body=build_report_html(cid, report_date, report_type, summary_text, report_data),
                    ContentType="text/html; charset=utf-8",
                )
            except Exception as e:
                print(f"[report_generator] HTML render/put failed for {cid}: {e}")
```

(Match the existing import style — the handler may import collectors as `from report_html import ...` or `from .report_html import ...`; check how `app_config` is imported in this handler and mirror it. The `try/except Exception` makes the HTML twin strictly best-effort.)

- [ ] **Step 4: Run tests.** `python -m pytest tests/unit/data_pipeline/ -q` → PASS. `python -m pytest tests/unit -q` → no regression.

- [ ] **Step 5: Commit.**

```bash
git add data-pipeline/report_generator/handler.py tests/unit/data_pipeline/
git commit -m "feat(reports): store HTML twin alongside the JSON report (best-effort)"
```

---

### Task 3: `/reports` HTML download (api + frontend)

**Files:**

- Modify: `api/reports/handler.py` (`GET /api/reports/{id}/html` → presigned URL for the `.html` key)
- Modify: `cdk/stacks/agent_stack.py` (api/reports route for `/{id}/html` if routes are per-path; + S3 get IAM if absent)
- Modify: `frontend/src/app/reports/page.tsx` or `frontend/src/components/reports/report-viewer.tsx` ("HTML 다운로드" button)
- Test: extend `tests/unit/api/` reports tests

**Interfaces:**

- Consumes: the `reports` row's `s3_key`. Produces: a presigned GET URL for the `.html` twin.

- [ ] **Step 1: Read** `api/reports/handler.py` (the GET-by-id arm + how it reads `s3_key`), the api/reports route registration in `cdk/stacks/agent_stack.py` (is it `/api/reports` + `/api/reports/{id}` — add `/api/reports/{id}/html`?), the reports Lambda's S3 IAM, and `frontend/src/components/reports/report-viewer.tsx` (where to add the button) + the api-client.

- [ ] **Step 2: Write the failing test.** api/reports unit: `GET /api/reports/{id}/html` for a report whose `.html` exists (mock S3 head_object ok + generate_presigned_url) → 200 with a `url`; `.html` absent (head_object 404) → 404 with a Korean note. Mirror the existing api/reports test harness.

- [ ] **Step 3: Implement the download arm.** In `api/reports/handler.py`, add a path arm: for `GET /api/reports/{id}/html`, load the row's `s3_key`, derive `html_key = s3_key[:-5]+".html"`, `head_object` (404 + note if missing), else `generate_presigned_url("get_object", ..., ExpiresIn=300)` → `{"url": ...}`. Read-only.

- [ ] **Step 4: Route + IAM.** Add the `/api/reports/{id}/html` route (`cdk/stacks/agent_stack.py`) if routes are per-path. Confirm the reports Lambda has `s3:GetObject` on the archive bucket (the JSON read path likely already grants it; add only if absent).

- [ ] **Step 5: Frontend button.** Add an "HTML 다운로드" button in the report viewer that calls `/api/reports/{id}/html` → opens the returned `url` (new tab / download). Disable + tooltip when the call 404s (pre-HTML report). Korean label.

- [ ] **Step 6: Run + build.** `python -m pytest tests/unit/api/ -q` → PASS; `python -m pytest tests/unit -q` → no regression; `python -m pytest tests/cdk/test_synth.py -q` → PASS; `cd frontend && npm run build` → PASS. If a new route was added, `python tools/openapi_gen.py` + commit the openapi.json (route-table parity).

- [ ] **Step 7: Commit.**

```bash
git add api/reports/handler.py cdk/stacks/agent_stack.py frontend/ tests/unit/api/ frontend/public/openapi.json
git commit -m "feat(reports): /reports HTML download (presigned) + viewer button"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- Final whole-branch review (most capable model) — focus: HTML is self-contained (no external refs/scripts); ALL DB/AI text HTML-escaped (no injection); HTML twin is best-effort (JSON/DB/delivery unaffected on failure); the `.html` key derivation matches both sides (generator writes, api derives the same key); download is read-only presigned + 404-on-missing for old reports; no new dep, no schema migration; openapi parity if a route was added.
- Deploy dev: `cdk deploy dbops-dev-data` (report_generator) + `cdk deploy dbops-dev-agent` (api/reports + route). Frontend build → sync → invalidate `E1234567890ABC`.
- Live smoke: trigger report_generator (direct Lambda invoke) for a cluster with data → confirm a `.html` lands in S3; `GET /api/reports/{id}/html` → 200 + a presigned url that fetches valid self-contained HTML; an old report → 404 + note. No-regression: the existing JSON report viewer + delivery still work.
- Then `superpowers:finishing-a-development-branch`.
