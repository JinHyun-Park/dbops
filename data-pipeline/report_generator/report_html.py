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
    vals = []
    for v in (values or []):
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            pass
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
    aas = data.get("aas") or {}
    top_slow_queries = data.get("top_slow_queries") or []
    top_alerts = data.get("top_alerts") or []
    connections = data.get("connections") or {}

    spark = sparkline([p.get("value") for p in aas_series])
    cards = ""
    if aas.get("avg_aas") is not None:
        cards += (f'<div class="card"><div class="card-lbl">AAS 평균</div>'
                  f'<div class="card-val">{_fmt(aas["avg_aas"])}</div>{spark}</div>')
    if aas.get("max_aas") is not None:
        cards += (f'<div class="card"><div class="card-lbl">AAS 최대</div>'
                  f'<div class="card-val">{_fmt(aas["max_aas"])}</div>{spark}</div>')
    if connections.get("max_conn") is not None:
        cards += (f'<div class="card"><div class="card-lbl">최대 연결 수</div>'
                  f'<div class="card-val">{_fmt(connections["max_conn"])}</div></div>')

    query_rows = [
        {"label": (q.get("query_excerpt") or ""), "value": q.get("total_ms") or 0}
        for q in top_slow_queries
    ]

    alerts_rows = "".join(
        f'<tr><td>{escape(str(a.get("rule_id") or ""))}</td>'
        f'<td>{escape(str(a.get("fired_count") or ""))}</td>'
        f'<td>{escape(str(a.get("last_fired") or ""))}</td></tr>'
        for a in top_alerts
    ) or '<tr><td colspan="3">발생한 알림 없음</td></tr>'

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
<div class="section"><h2>상위 쿼리</h2>{bar_chart(query_rows, "총 실행시간(ms)")}</div>
<div class="section"><h2>알림</h2>
<table><tr><th>규칙</th><th>발생 횟수</th><th>마지막 발생</th></tr>{alerts_rows}</table></div>
</body></html>"""


def _fmt_bytes(n):
    try:
        f = float(n)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if f < 0 else ("+" if f > 0 else "")
    f = abs(f)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{sign}{f:.1f} {unit}"
        f /= 1024
    return f"{sign}{f:.1f} PB"


def _cluster_row_cells(r):
    return (
        f'<td>{escape(str(r.get("cluster_id") or ""))}</td>'
        f'<td>{escape(str(r.get("engine") or ""))}</td>'
        f'<td>{escape(str(r.get("health") or ""))}</td>'
        f'<td>{_fmt(r.get("aas_avg"))}</td>'
        f'<td>{_fmt(r.get("aas_max"))}</td>'
        f'<td>{_fmt(r.get("alert_count"))}</td>'
        f'<td>{_fmt(r.get("slow_query_count"))}</td>'
        f'<td>{_fmt_bytes(r.get("storage_delta_bytes"))}</td>'
    )


def build_fleet_report_html(report_date, report_type, summary, fleet_data):
    """Self-contained fleet rollup HTML — same inline-SVG/no-deps style as
    build_report_html. Renders across all clusters, not one."""
    fleet_data = fleet_data or {}
    totals = fleet_data.get("totals") or {}
    engine_counts = fleet_data.get("engine_counts") or {}
    health_dist = fleet_data.get("health_distribution") or {}
    worst = fleet_data.get("worst_clusters") or []
    clusters = fleet_data.get("clusters") or []

    cards = (
        f'<div class="card"><div class="card-lbl">클러스터 수</div>'
        f'<div class="card-val">{_fmt(fleet_data.get("clusters_total"))}</div></div>'
        f'<div class="card"><div class="card-lbl">총 경보</div>'
        f'<div class="card-val">{_fmt(totals.get("alerts"))}</div></div>'
        f'<div class="card"><div class="card-lbl">총 슬로우 쿼리</div>'
        f'<div class="card-val">{_fmt(totals.get("slow_queries"))}</div></div>'
    )

    engine_badges = "".join(
        f'<span style="background:#27272a;color:#e4e4e7;border-radius:9px;'
        f'padding:2px 10px;font-size:12px;margin-right:6px">{escape(str(k))} · {_fmt(v)}</span>'
        for k, v in engine_counts.items()
    ) or '<span style="color:#71717a;font-size:12px">엔진 정보 없음</span>'

    health_bars = bar_chart(
        [{"label": k, "count": v} for k, v in health_dist.items()], "상태 분포"
    )

    worst_rows = "".join(f"<tr>{_cluster_row_cells(r)}</tr>" for r in worst) \
        or '<tr><td colspan="8">데이터 없음</td></tr>'
    cluster_rows = "".join(f"<tr>{_cluster_row_cells(r)}</tr>" for r in clusters) \
        or '<tr><td colspan="8">데이터 없음</td></tr>'
    thead = ("<tr><th>클러스터</th><th>엔진</th><th>상태</th><th>AAS avg</th>"
             "<th>AAS max</th><th>경보</th><th>슬로우</th><th>스토리지 Δ</th></tr>")

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DBOps Fleet 리포트 — Fleet 전체 {escape(str(report_date))}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#18181b;margin:0;padding:24px;background:#fafafa}}
h1{{font-size:20px;margin:0 0 4px}} .meta{{color:#71717a;font-size:13px;margin-bottom:20px}}
.summary{{background:#fff;border:1px solid #e4e4e7;border-radius:8px;padding:16px;white-space:pre-wrap;line-height:1.6;margin-bottom:20px}}
.cards{{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
.card{{background:#fff;border:1px solid #e4e4e7;border-radius:8px;padding:12px 16px;min-width:120px}}
.card-lbl{{color:#71717a;font-size:12px}} .card-val{{font-size:22px;font-weight:600}}
.section{{background:#fff;border:1px solid #e4e4e7;border-radius:8px;padding:16px;margin-bottom:20px}}
.section h2{{font-size:14px;margin:0 0 12px;color:#3f3f46}}
.table-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #f4f4f5;white-space:nowrap}}
</style></head><body>
<h1>DBOps Fleet 운영 리포트</h1>
<div class="meta">Fleet 전체 · {escape(str(report_date))} · {escape(str(report_type))}</div>
<div class="cards">{cards}</div>
<div class="summary">{escape(str(summary or ''))}</div>
<div class="section"><h2>엔진 분포</h2>{engine_badges}</div>
<div class="section"><h2>상태 분포</h2>{health_bars}</div>
<div class="section"><h2>주의가 필요한 클러스터 (Top 5)</h2>
<div class="table-wrap"><table>{thead}{worst_rows}</table></div></div>
<div class="section"><h2>전체 클러스터</h2>
<div class="table-wrap"><table>{thead}{cluster_rows}</table></div></div>
</body></html>"""
