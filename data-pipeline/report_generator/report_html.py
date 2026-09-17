"""Pure-Python self-contained HTML report builder with inline SVG charts.

No third-party deps (no Lambda bundle change), no external fonts/scripts/images
(privacy + offline + attachable). All DB/AI-derived text is HTML-escaped."""

from html import escape

_W, _H, _PAD = 640, 180, 28  # chart viewBox

# Hangul syllables. Enough to tell the two languages this product ships apart,
# and the same test frontend/src/lib/narrative-language.ts uses to label stored
# model prose, for the same input.

# The document's own chrome: titles, card labels, section headings, table
# headers, placeholders. The SUMMARY inside it follows the deployment locale
# (handler._report_locale), so a Korean-only shell produced a document whose
# English prose sat in Korean furniture under lang="ko".
#
# Same contract as the frontend's en.ts: an exact-equality lookup keyed by the
# Korean SOURCE string, with the key itself as the fallback, so a missing entry
# degrades to Korean instead of throwing. Kept here rather than shared because
# data-pipeline cannot import the frontend.
#
# DBA-facing jargon stays English in both locales (AAS avg, AAS max, the Δ, the
# metric names), which is this project's standing translation rule, so those are
# not keys. Engine names are not keys either: they are stored identifiers.
#
# The health column's VALUES are the one exception, and it is deliberate. They
# are a CLOSED set the collectors write, so they behave like chrome even though
# they arrive in a row, and leaving them out was measured: an English fleet
# document contained exactly two Hangul words, both of them health cells.
_EN = {
    # shared placeholders
    "데이터 없음": "No data",
    "발생한 알림 없음": "No alerts fired",
    "엔진 정보 없음": "No engine information",
    # per-cluster report
    "DBOps 리포트": "DBOps report",
    "DBOps 운영 리포트": "DBOps operations report",
    "AAS 평균": "AAS avg",
    "AAS 최대": "AAS max",
    "최대 연결 수": "Peak connections",
    "활동 추이 (AAS)": "Activity trend (AAS)",
    "상위 쿼리": "Top queries",
    "총 실행시간(ms)": "Total time (ms)",
    "알림": "Alerts",
    "규칙": "Rule",
    "발생 횟수": "Fired",
    "마지막 발생": "Last fired",
    # fleet rollup
    "DBOps Fleet 리포트": "DBOps fleet report",
    "DBOps Fleet 운영 리포트": "DBOps fleet operations report",
    "Fleet 전체": "Whole fleet",
    "클러스터 수": "Clusters",
    "총 경보": "Total alerts",
    "총 슬로우 쿼리": "Total slow queries",
    "엔진 분포": "Engine mix",
    "상태 분포": "Health mix",
    "주의가 필요한 클러스터 (Top 5)": "Clusters needing attention (top 5)",
    "전체 클러스터": "All clusters",
    "클러스터": "Cluster",
    "엔진": "Engine",
    "상태": "Status",
    "경보": "Alerts",
    "슬로우": "Slow",
    "스토리지 Δ": "Storage Δ",
    # The health column's VALUES, not chrome. They are a closed set that the
    # collectors write (only these two exist today), and the English values are
    # the ones frontend/src/lib/messages/en.ts already uses for the same scale,
    # so one report and the console cannot disagree about what a row says.
    # Anything outside the set falls through _t unchanged, which is visible
    # rather than wrong.
    "정상": "OK",
    "주의": "Warning",
}


def _t(loc, ko):
    """Translate one shell string. Anything but "en" reads Korean, the language
    every deployment ships, matching handler._report_locale's fail-closed
    allowlist.

    Every string in _EN is code-authored, so the locale that picked it IS the
    language it is in. That is what lets `lang` follow the locale too (see
    _lang). The summary is the one part of the document with no such guarantee.
    """
    return _EN.get(ko, ko) if loc == "en" else ko


def _lang(loc, summary=""):
    """The `lang` attribute, taken from the CALLER'S LOCALE, not from the prose.

    It scopes the WHOLE document, and the whole document apart from one <div> is
    chrome: the title, the meta line, the card labels, the section headings and
    every table header, all of them code-authored and all of them in `loc` by
    construction. So `loc` is right for the overwhelming majority of the text
    nodes this attribute governs.

    Reading it off the summary instead was tried and MEASURED WRONG twice over.
    (1) An English summary routinely contains Hangul, because
    _build_summary_prompt feeds the model up to 120 characters of raw
    query_excerpt plus the Korean section labels, so a summary that merely
    quotes a customer's SQL literal (status = '배송중') or echoes a label scored
    "ko" while the document was English. (2) Even when the script test is right
    about the summary, it is then wrong about the chrome, which is more of the
    document: a ko deployment whose model answered in English got lang="en" over
    Korean headings, and a screen reader reads those with English phonology.

    `summary` is still accepted, and deliberately unused, so the call sites keep
    documenting that the summary's language is a SEPARATE question. It is
    answered where it can be answered honestly: the frontend labels the summary
    from its own text (narrative-language.summaryLanguage), against the console
    locale, at the one place a reader sees the two side by side. This attribute
    makes no claim about it.
    """
    del summary  # see the docstring: the prose's language is not this decision
    return "en" if loc == "en" else "ko"


def _fmt(n):
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{f:,.2f}".rstrip("0").rstrip(".") if f % 1 else f"{int(f):,}"


def _placeholder(loc="ko", msg="데이터 없음"):
    msg = _t(loc, msg)
    return (f'<svg viewBox="0 0 {_W} {_H}" width="100%" role="img">'
            f'<rect width="{_W}" height="{_H}" fill="#f4f4f5"/>'
            f'<text x="{_W//2}" y="{_H//2}" text-anchor="middle" fill="#71717a" '
            f'font-family="sans-serif" font-size="14">{escape(msg)}</text></svg>')


def line_chart(points, label="", loc="ko"):
    """points: list of {ts, value}. Renders a simple line over the value series."""
    vals = []
    for p in (points or []):
        try:
            vals.append(float(p.get("value")))
        except (TypeError, ValueError):
            pass
    if len(vals) < 2:
        return _placeholder(loc)
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


def bar_chart(rows, label="", loc="ko"):
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
        return _placeholder(loc)
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


def build_report_html(cluster_id, report_date, report_type, summary, data, locale):
    """`locale` is the language the SUMMARY was generated in, and it is required
    on purpose: the shell has to agree with the prose it wraps, and a default
    would let a caller ship an English summary in a Korean document again."""
    data = data or {}
    aas_series = data.get("aas_series") or []
    aas = data.get("aas") or {}
    top_slow_queries = data.get("top_slow_queries") or []
    top_alerts = data.get("top_alerts") or []
    connections = data.get("connections") or {}

    spark = sparkline([p.get("value") for p in aas_series])
    cards = ""
    if aas.get("avg_aas") is not None:
        cards += (f'<div class="card"><div class="card-lbl">{_t(locale, "AAS 평균")}</div>'
                  f'<div class="card-val">{_fmt(aas["avg_aas"])}</div>{spark}</div>')
    if aas.get("max_aas") is not None:
        cards += (f'<div class="card"><div class="card-lbl">{_t(locale, "AAS 최대")}</div>'
                  f'<div class="card-val">{_fmt(aas["max_aas"])}</div>{spark}</div>')
    if connections.get("max_conn") is not None:
        cards += (f'<div class="card"><div class="card-lbl">{_t(locale, "최대 연결 수")}</div>'
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
    ) or f'<tr><td colspan="3">{_t(locale, "발생한 알림 없음")}</td></tr>'

    return f"""<!doctype html>
<html lang="{_lang(locale, summary)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_t(locale, "DBOps 리포트")}: {escape(str(cluster_id))} {escape(str(report_date))}</title>
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
<h1>{_t(locale, "DBOps 운영 리포트")}</h1>
<div class="meta">{escape(str(cluster_id))}, {escape(str(report_date))}, {escape(str(report_type))}</div>
<div class="cards">{cards or ''}</div>
<div class="summary">{escape(str(summary or ''))}</div>
<div class="section"><h2>{_t(locale, "활동 추이 (AAS)")}</h2>{line_chart(aas_series, "AAS", locale)}</div>
<div class="section"><h2>{_t(locale, "상위 쿼리")}</h2>{bar_chart(query_rows, _t(locale, "총 실행시간(ms)"), locale)}</div>
<div class="section"><h2>{_t(locale, "알림")}</h2>
<table><tr><th>{_t(locale, "규칙")}</th><th>{_t(locale, "발생 횟수")}</th><th>{_t(locale, "마지막 발생")}</th></tr>{alerts_rows}</table></div>
</body></html>"""


def _fmt_bytes(n):
    try:
        f = float(n)
    except (TypeError, ValueError):
        # An EMPTY cell, not the repo's usual "-" no-data placeholder: the very
        # next line uses "-" as this formatter's NEGATIVE SIGN, so returning it
        # here would render "no data" and a truncated negative identically.
        return ""
    sign = "-" if f < 0 else ("+" if f > 0 else "")
    f = abs(f)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{sign}{f:.1f} {unit}"
        f /= 1024
    return f"{sign}{f:.1f} PB"


def _fleet_thead(loc):
    return ("<tr>"
            f'<th>{_t(loc, "클러스터")}</th><th>{_t(loc, "엔진")}</th>'
            f'<th>{_t(loc, "상태")}</th><th>AAS avg</th><th>AAS max</th>'
            f'<th>{_t(loc, "경보")}</th><th>{_t(loc, "슬로우")}</th>'
            f'<th>{_t(loc, "스토리지 Δ")}</th></tr>')


def _cluster_row_cells(r, locale):
    health = str(r.get("health") or "")
    return (
        f'<td>{escape(str(r.get("cluster_id") or ""))}</td>'
        f'<td>{escape(str(r.get("engine") or ""))}</td>'
        f'<td>{escape(_t(locale, health) if health else "")}</td>'
        f'<td>{_fmt(r.get("aas_avg"))}</td>'
        f'<td>{_fmt(r.get("aas_max"))}</td>'
        f'<td>{_fmt(r.get("alert_count"))}</td>'
        f'<td>{_fmt(r.get("slow_query_count"))}</td>'
        f'<td>{_fmt_bytes(r.get("storage_delta_bytes"))}</td>'
    )


def build_fleet_report_html(report_date, report_type, summary, fleet_data, locale):
    """Self-contained fleet rollup HTML, same inline-SVG/no-deps style as
    build_report_html. Renders across all clusters, not one. `locale` is
    required for the same reason it is there (see build_report_html)."""
    fleet_data = fleet_data or {}
    totals = fleet_data.get("totals") or {}
    engine_counts = fleet_data.get("engine_counts") or {}
    health_dist = fleet_data.get("health_distribution") or {}
    worst = fleet_data.get("worst_clusters") or []
    clusters = fleet_data.get("clusters") or []

    cards = (
        f'<div class="card"><div class="card-lbl">{_t(locale, "클러스터 수")}</div>'
        f'<div class="card-val">{_fmt(fleet_data.get("clusters_total"))}</div></div>'
        f'<div class="card"><div class="card-lbl">{_t(locale, "총 경보")}</div>'
        f'<div class="card-val">{_fmt(totals.get("alerts"))}</div></div>'
        f'<div class="card"><div class="card-lbl">{_t(locale, "총 슬로우 쿼리")}</div>'
        f'<div class="card-val">{_fmt(totals.get("slow_queries"))}</div></div>'
    )

    engine_badges = "".join(
        f'<span style="background:#27272a;color:#e4e4e7;border-radius:9px;'
        f'padding:2px 10px;font-size:12px;margin-right:6px">{escape(str(k))}: {_fmt(v)}</span>'
        for k, v in engine_counts.items()
    ) or ('<span style="color:#71717a;font-size:12px">'
          f'{_t(locale, "엔진 정보 없음")}</span>')

    # The distribution is keyed by the same closed health set the table cells
    # carry (handler._generate_fleet_rollup counts "주의" / "정상"), so the bar
    # LABELS need the same lookup the cells get.
    health_bars = bar_chart(
        [{"label": _t(locale, str(k)), "count": v} for k, v in health_dist.items()],
        _t(locale, "상태 분포"), locale,
    )

    no_data = f'<tr><td colspan="8">{_t(locale, "데이터 없음")}</td></tr>'
    worst_rows = "".join(f"<tr>{_cluster_row_cells(r, locale)}</tr>" for r in worst) or no_data
    cluster_rows = "".join(f"<tr>{_cluster_row_cells(r, locale)}</tr>" for r in clusters) or no_data
    thead = _fleet_thead(locale)

    return f"""<!doctype html>
<html lang="{_lang(locale, summary)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_t(locale, "DBOps Fleet 리포트")}: {_t(locale, "Fleet 전체")} {escape(str(report_date))}</title>
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
<h1>{_t(locale, "DBOps Fleet 운영 리포트")}</h1>
<div class="meta">{_t(locale, "Fleet 전체")}, {escape(str(report_date))}, {escape(str(report_type))}</div>
<div class="cards">{cards}</div>
<div class="summary">{escape(str(summary or ''))}</div>
<div class="section"><h2>{_t(locale, "엔진 분포")}</h2>{engine_badges}</div>
<div class="section"><h2>{_t(locale, "상태 분포")}</h2>{health_bars}</div>
<div class="section"><h2>{_t(locale, "주의가 필요한 클러스터 (Top 5)")}</h2>
<div class="table-wrap"><table>{thead}{worst_rows}</table></div></div>
<div class="section"><h2>{_t(locale, "전체 클러스터")}</h2>
<div class="table-wrap"><table>{thead}{cluster_rows}</table></div></div>
</body></html>"""
