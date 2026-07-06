"""report_generator — generate per-cluster daily/weekly operations summaries.

The earlier version of this Lambda wrote a literal one-line string
("daily report for X on YYYY-MM-DD") into the `summary` column and
called it done. That's information-free.

This version produces a real operations report:

  - structured JSON in `data` column: AAS percentiles, peak time +
    duration above threshold, top 5 slow queries by total_time, top 5
    alert rules fired, storage delta, connection peak, event counts
  - NL summary (Bedrock Claude) in `summary` column: 3–5 sentences that
    a DBA could skim during morning standup

The Bedrock call is best-effort. If invocation fails (throttling, model
unavailable, no permission) we fall back to a deterministic template
summary so the report row still has *something* useful in the summary
column.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import boto3
from app_config import get_config
from botocore.exceptions import ClientError

# Threshold above which we count "AAS minutes" — i.e. how long the
# cluster spent in a notably busy state.
AAS_BUSY_THRESHOLD = float(os.environ.get("REPORT_AAS_BUSY_THRESHOLD", "5"))

# Which Bedrock model writes the NL summary. Default mirrors the agent's
# model (apac.anthropic.claude-sonnet-4-...) but operators can swap to a
# cheaper Haiku via env var if cost matters more than prose quality.
SUMMARY_MODEL_ID = os.environ.get(
    "REPORT_SUMMARY_MODEL_ID",
    "apac.anthropic.claude-sonnet-4-20250514-v1:0",
)


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    s3_bucket = os.environ.get("ARCHIVE_BUCKET", "")

    def cache_query(sql: str, params: dict | None = None) -> list[dict]:
        sql_params = []
        if params:
            for k, v in params.items():
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn, secretArn=secret_arn, database=database,
            sql=f"/* source=dbops-report */ {sql}", parameters=sql_params,
            includeResultMetadata=True,
        )
        cols = [c["name"] for c in resp.get("columnMetadata", [])]
        rows = []
        for rec in resp.get("records", []):
            row: dict[str, Any] = {}
            for i, f in enumerate(rec):
                col = cols[i] if i < len(cols) else f"col_{i}"
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        row[col] = f[typ]
                        break
                else:
                    row[col] = None
            rows.append(row)
        return rows

    clusters = cache_query("SELECT cluster_id, engine FROM cluster_meta")
    report_date = datetime.utcnow().strftime("%Y-%m-%d")
    report_type = event.get("report_type", "daily")
    reports_generated = []
    fleet_rows = []  # compact per-cluster records for the fleet rollup

    for cluster in clusters:
        cid = cluster["cluster_id"]
        report_data = _build_report_data(cache_query, cid)
        summary_text = _write_nl_summary(cid, report_date, report_data)
        fleet_rows.append(_fleet_row(cid, cluster.get("engine"), report_data))

        s3_key = f"reports/{cid}/{report_date}-{report_type}.json"
        if s3_bucket:
            try:
                boto3.client("s3").put_object(
                    Bucket=s3_bucket, Key=s3_key,
                    Body=json.dumps(report_data, default=str),
                    ContentType="application/json",
                )
            except ClientError as e:
                print(f"[report_generator] S3 put failed for {cid}: {e}")

        if s3_bucket:
            try:
                from report_html import build_report_html
                html_key = s3_key[:-5] + ".html" if s3_key.endswith(".json") else s3_key + ".html"
                boto3.client("s3").put_object(
                    Bucket=s3_bucket, Key=html_key,
                    Body=build_report_html(cid, report_date, report_type, summary_text, report_data),
                    ContentType="text/html; charset=utf-8",
                )
            except Exception as e:
                print(f"[report_generator] HTML render/put failed for {cid}: {e}")

        cache_query(
            # RDS Data API parameters arrive as strings; cast :report_date
            # to DATE explicitly so PostgreSQL accepts it for the DATE column.
            "INSERT INTO reports (cluster_id, report_type, report_date, summary, data, s3_key) "
            "VALUES (:cid, :report_type, (:report_date)::date, :summary, :data::jsonb, :s3_key)",
            {
                "cid": cid,
                "report_type": report_type,
                "report_date": report_date,
                "summary": summary_text,
                "data": json.dumps(report_data, default=str),
                "s3_key": s3_key,
            },
        )
        _deliver_report(cache_query, cid, report_date, report_type, summary_text)
        reports_generated.append(cid)

    # Fleet rollup: one report across all clusters, generated after the
    # per-cluster loop. Best-effort — a rollup failure must never fail the
    # per-cluster reports already written. Skip entirely when 0 clusters.
    if fleet_rows:
        try:
            _generate_fleet_rollup(
                cache_query, s3_bucket, report_date, report_type, fleet_rows
            )
            reports_generated.append("*")
        except Exception as e:
            print(f"[report_generator] fleet rollup failed: {type(e).__name__}: {e}")

    return {
        "statusCode": 200,
        "body": json.dumps({"reports": reports_generated, "date": report_date}),
    }


def _build_report_data(cache_query, cluster_id: str) -> dict:
    """Collect every numeric signal we can over the last 24h and return a
    structured dict that the UI renders as cards + lists."""
    aas_stats = cache_query(
        "SELECT AVG(value) AS avg_aas, MAX(value) AS max_aas, "
        "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95_aas, "
        "COUNT(*) AS samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'aas' "
        "AND ts > NOW() - INTERVAL '24 hours'",
        {"cid": cluster_id},
    )
    aas_peak = cache_query(
        "SELECT ts, value FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'aas' "
        "AND ts > NOW() - INTERVAL '24 hours' "
        "ORDER BY value DESC NULLS LAST LIMIT 1",
        {"cid": cluster_id},
    )
    aas_busy_minutes = cache_query(
        # RDS Data API parameters arrive as stringValue, so PostgreSQL sees
        # value > '5' and rejects the heterogeneous comparison. Cast the
        # parameter to double precision explicitly.
        "SELECT COUNT(*) AS cnt FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'aas' "
        "AND value > (:threshold)::double precision "
        "AND ts > NOW() - INTERVAL '24 hours'",
        {"cid": cluster_id, "threshold": str(AAS_BUSY_THRESHOLD)},
    )
    aas_series = cache_query(
        # Per-sample cluster-level AAS over the window for the report's line chart.
        # Filter out per-instance dimensioned rows so the series is the cluster
        # aggregate, not a mix of instances (same guard as the connections query).
        "SELECT ts, value FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'aas' "
        "AND ts > NOW() - INTERVAL '24 hours' "
        "AND (dimensions IS NULL OR NOT jsonb_exists(dimensions, 'instance')) "
        "ORDER BY ts ASC",
        {"cid": cluster_id},
    )
    # query_stats is the pg_stat_statements-derived table that actually has
    # call counts + total/mean execution time. The slow_queries table is a
    # raw log of individual slow executions and lacks the aggregated columns
    # we want here.
    top_slow = cache_query(
        "SELECT query_hash, LEFT(query_text, 200) AS query_excerpt, "
        "MAX(calls) AS calls, MAX(total_time_ms) AS total_ms, MAX(mean_time_ms) AS mean_ms "
        "FROM query_stats "
        "WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '24 hours' "
        "GROUP BY query_hash, query_text "
        "ORDER BY total_ms DESC NULLS LAST LIMIT 5",
        {"cid": cluster_id},
    )
    # Alerts live in event_log with event_type='alert'. rule_id is buried
    # inside raw_event JSONB — extract it via the ->> operator so we can
    # group by rule.
    top_alerts = cache_query(
        "SELECT raw_event->>'rule_id' AS rule_id, "
        "COUNT(*) AS fired_count, MAX(event_time) AS last_fired "
        "FROM event_log "
        "WHERE cluster_id = :cid AND event_type = 'alert' "
        "AND event_time > NOW() - INTERVAL '24 hours' "
        "GROUP BY raw_event->>'rule_id' "
        "ORDER BY fired_count DESC LIMIT 5",
        {"cid": cluster_id},
    )
    storage_delta = cache_query(
        "WITH endpoints AS ( "
        "  SELECT "
        "    (SELECT value FROM metric_snapshots "
        "       WHERE cluster_id = :cid AND metric_type = 'storage_bytes' "
        "       AND ts > NOW() - INTERVAL '24 hours' "
        "       ORDER BY ts ASC LIMIT 1) AS start_bytes, "
        "    (SELECT value FROM metric_snapshots "
        "       WHERE cluster_id = :cid AND metric_type = 'storage_bytes' "
        "       ORDER BY ts DESC LIMIT 1) AS end_bytes "
        ") SELECT start_bytes, end_bytes, (end_bytes - start_bytes) AS delta_bytes "
        "FROM endpoints",
        {"cid": cluster_id},
    )
    conn_peak = cache_query(
        # Canonical total-connections metric = db_connections (CloudWatch
        # DatabaseConnections), populated for every cluster — the PI-only
        # "connections" was empty when Performance Insights was off, leaving
        # the report's connection peak blank.
        "SELECT MAX(value) AS max_conn, AVG(value) AS avg_conn FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'db_connections' "
        "AND ts > NOW() - INTERVAL '24 hours' "
        "AND (dimensions IS NULL OR NOT jsonb_exists(dimensions, 'instance'))",
        {"cid": cluster_id},
    )
    events_by_type = cache_query(
        "SELECT event_type, COUNT(*) AS cnt FROM event_log "
        "WHERE cluster_id = :cid AND event_time > NOW() - INTERVAL '24 hours' "
        "GROUP BY event_type ORDER BY cnt DESC LIMIT 10",
        {"cid": cluster_id},
    )

    return {
        "cluster_id": cluster_id,
        "window_hours": 24,
        "aas": aas_stats[0] if aas_stats else {},
        "aas_peak": aas_peak[0] if aas_peak else {},
        "aas_busy_minutes_above_threshold": (aas_busy_minutes[0].get("cnt") if aas_busy_minutes else 0),
        "aas_busy_threshold": AAS_BUSY_THRESHOLD,
        "aas_series": aas_series,
        "top_slow_queries": top_slow,
        "top_alerts": top_alerts,
        "storage": storage_delta[0] if storage_delta else {},
        "connections": conn_peak[0] if conn_peak else {},
        "events_by_type": events_by_type,
    }


def _write_nl_summary(cluster_id: str, report_date: str, data: dict) -> str:
    """Ask Bedrock to write a 3–5 sentence DBA-readable summary. On any
    error, fall back to a deterministic template so the report row still
    has a usable summary."""
    try:
        bedrock = boto3.client("bedrock-runtime")
        prompt = _build_summary_prompt(cluster_id, report_date, data)
        resp = bedrock.invoke_model(
            modelId=SUMMARY_MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        body = json.loads(resp["body"].read())
        text = (body.get("content") or [{}])[0].get("text", "").strip()
        if text:
            return text
    except Exception as e:
        # Throttling, IAM, model unavailable — all land here. Falling
        # back is fine; the structured data column still has the numbers.
        print(f"[report_generator] Bedrock summary failed for {cluster_id}: {e}")

    return _template_summary(cluster_id, report_date, data)


def _build_summary_prompt(cluster_id: str, report_date: str, data: dict) -> str:
    aas = data.get("aas") or {}
    peak = data.get("aas_peak") or {}
    storage = data.get("storage") or {}
    conns = data.get("connections") or {}
    busy_min = data.get("aas_busy_minutes_above_threshold") or 0

    slow_lines = []
    for i, q in enumerate(data.get("top_slow_queries") or [], 1):
        slow_lines.append(
            f"  {i}. total {q.get('total_ms', 0):.0f}ms over {q.get('calls', 0)} calls — "
            f"{(q.get('query_excerpt') or '').strip()[:120]}"
        )
    slow_block = "\n".join(slow_lines) if slow_lines else "  (none)"

    alert_lines = []
    for i, a in enumerate(data.get("top_alerts") or [], 1):
        alert_lines.append(f"  {i}. rule_id={a.get('rule_id')} fired {a.get('fired_count')}x")
    alert_block = "\n".join(alert_lines) if alert_lines else "  (none)"

    storage_delta_mb = ((storage.get("delta_bytes") or 0) / (1024 * 1024)) if storage else 0

    return (
        f"당신은 시니어 DBA 입니다. Aurora 클러스터 {cluster_id} 의 지난 24시간 운영 요약을 "
        "한국어 3~5문장으로 작성하세요. 핵심 변화만 짚고, 평소 운영 범위 안의 수치는 굳이 언급하지 마세요. "
        "리스트/마크다운 헤더 없이 평문으로 쓰세요.\n\n"
        f"## {report_date} 메트릭 요약\n"
        f"- AAS avg={aas.get('avg_aas')}, max={aas.get('max_aas')}, p95={aas.get('p95_aas')}\n"
        f"- AAS 피크: {peak.get('value')} @ {peak.get('ts')}\n"
        f"- AAS > {data.get('aas_busy_threshold')} 인 1분 샘플 수: {busy_min}\n"
        f"- 활성 연결 max={conns.get('max_conn')}, avg={conns.get('avg_conn')}\n"
        f"- 스토리지 변화: {storage_delta_mb:.1f} MB ({storage.get('start_bytes')} → {storage.get('end_bytes')})\n"
        f"- Top slow queries:\n{slow_block}\n"
        f"- Top alert rules:\n{alert_block}\n"
        f"- 이벤트 타입별 카운트: {data.get('events_by_type')}\n"
    )


def _template_summary(cluster_id: str, report_date: str, data: dict) -> str:
    """Deterministic fallback when Bedrock is unreachable. Less polished
    than the LLM version but informative."""
    aas = data.get("aas") or {}
    busy_min = data.get("aas_busy_minutes_above_threshold") or 0
    top = data.get("top_slow_queries") or []
    alerts = data.get("top_alerts") or []
    pieces = [f"{cluster_id} 24시간 요약 ({report_date})"]
    if aas:
        pieces.append(
            f"AAS avg={float(aas.get('avg_aas') or 0):.2f}, "
            f"max={float(aas.get('max_aas') or 0):.2f}, "
            f"AAS>{data.get('aas_busy_threshold')} 인 샘플 {busy_min}개."
        )
    if top:
        pieces.append(f"Top slow query total {float(top[0].get('total_ms') or 0):.0f}ms 누적.")
    if alerts:
        pieces.append(f"가장 자주 발화한 룰: {alerts[0].get('rule_id')} ({alerts[0].get('fired_count')}회).")
    if not (top or alerts):
        pieces.append("주목할 만한 이벤트는 없었습니다.")
    return " ".join(pieces)


def _fleet_row(cluster_id: str, engine, report_data: dict) -> dict:
    """Compact per-cluster record for the fleet rollup, built from the numbers
    _build_report_data already computed — NO extra queries.

    alert_count sums fired_count over the (top-5) alert rules; slow_query_count
    is the number of top slow queries surfaced (<=5). Both are report-scoped
    headline numbers, not exhaustive counts. Health is a coarse rollup bucket
    derived from alert_count (no severity is carried in the report data)."""
    aas = report_data.get("aas") or {}
    storage = report_data.get("storage") or {}
    alerts = report_data.get("top_alerts") or []
    total_alerts = sum(int(a.get("fired_count") or 0) for a in alerts)
    return {
        "cluster_id": cluster_id,
        "engine": engine or "unknown",
        "health": "주의" if total_alerts > 0 else "정상",
        "aas_avg": aas.get("avg_aas"),
        "aas_max": aas.get("max_aas"),
        "slow_query_count": len(report_data.get("top_slow_queries") or []),
        "alert_count": total_alerts,
        "storage_delta_bytes": (storage.get("delta_bytes") if storage else None),
    }


def _build_fleet_data(rows: list[dict]) -> dict:
    """Aggregate compact per-cluster records into the fleet rollup payload."""
    from collections import Counter

    engine_counts = dict(Counter((r.get("engine") or "unknown") for r in rows))
    health_dist = dict(Counter((r.get("health") or "정상") for r in rows))
    total_alerts = sum(int(r.get("alert_count") or 0) for r in rows)
    total_slow = sum(int(r.get("slow_query_count") or 0) for r in rows)
    worst = sorted(
        rows,
        key=lambda r: (int(r.get("alert_count") or 0), float(r.get("aas_max") or 0)),
        reverse=True,
    )[:5]
    return {
        "clusters_total": len(rows),
        "engine_counts": engine_counts,
        "health_distribution": health_dist,
        "totals": {"alerts": total_alerts, "slow_queries": total_slow},
        "worst_clusters": worst,
        "clusters": rows,
    }


def _fleet_summary(report_date: str, fleet_data: dict) -> str:
    """Deterministic Korean rollup summary — no Bedrock call. Mirrors the
    _template_summary style."""
    n = fleet_data.get("clusters_total", 0)
    totals = fleet_data.get("totals") or {}
    alerts = totals.get("alerts", 0)
    slow = totals.get("slow_queries", 0)
    worst = [
        w.get("cluster_id")
        for w in (fleet_data.get("worst_clusters") or [])
        if int(w.get("alert_count") or 0) > 0
    ]
    pieces = [
        f"Fleet 전체 요약 ({report_date})",
        f"클러스터 {n}대 · 경보 {alerts}건 · 슬로우쿼리 {slow}건.",
    ]
    if worst:
        pieces.append(f"주의가 필요한 클러스터: {', '.join(worst[:5])}.")
    else:
        pieces.append("주의가 필요한 클러스터는 없습니다.")
    return " ".join(pieces)


def _generate_fleet_rollup(cache_query, s3_bucket, report_date, report_type, fleet_rows):
    """Build + persist the fleet rollup exactly like a cluster report but with
    cluster_id='*'. Called best-effort by lambda_handler."""
    fleet_data = _build_fleet_data(fleet_rows)
    summary_text = _fleet_summary(report_date, fleet_data)

    s3_key = f"reports/_fleet/{report_date}-{report_type}.json"
    if s3_bucket:
        try:
            boto3.client("s3").put_object(
                Bucket=s3_bucket, Key=s3_key,
                Body=json.dumps(fleet_data, default=str),
                ContentType="application/json",
            )
        except ClientError as e:
            print(f"[report_generator] fleet S3 put failed: {e}")
        try:
            from report_html import build_fleet_report_html
            boto3.client("s3").put_object(
                Bucket=s3_bucket, Key=s3_key[:-5] + ".html",
                Body=build_fleet_report_html(report_date, report_type, summary_text, fleet_data),
                ContentType="text/html; charset=utf-8",
            )
        except Exception as e:
            print(f"[report_generator] fleet HTML render/put failed: {e}")

    cache_query(
        "INSERT INTO reports (cluster_id, report_type, report_date, summary, data, s3_key) "
        "VALUES (:cid, :report_type, (:report_date)::date, :summary, :data::jsonb, :s3_key)",
        {
            "cid": "*",
            "report_type": report_type,
            "report_date": report_date,
            "summary": summary_text,
            "data": json.dumps(fleet_data, default=str),
            "s3_key": s3_key,
        },
    )
    _deliver_report(cache_query, "*", report_date, report_type, summary_text)


def _post_json(url: str, payload: dict, timeout: int = 5) -> tuple[int, str]:
    """POST JSON to a webhook URL. Returns (status_code, body_excerpt)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "dbops-report-generator/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(512).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(512).decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
    except Exception as e:
        return 0, str(e)[:200]


def _build_report_slack_blocks(cluster_id, report_date, report_type, summary):
    return {
        "text": f"DBOps 리포트 · {cluster_id}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"📋 DBOps 리포트 · {report_date}"}},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*클러스터* `{cluster_id}` · *유형* {report_type}\n\n{summary[:2800]}"}},
        ],
    }


def _build_report_teams_card(cluster_id, report_date, report_type, summary) -> dict:
    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": f"DBOps 리포트 · {cluster_id}",
        "themeColor": "2563EB",
        "title": f"\U0001f4cb DBOps 리포트 · {report_date}",
        "sections": [{
            "facts": [
                {"name": "클러스터", "value": f"`{cluster_id}`"},
                {"name": "유형", "value": str(report_type)},
            ],
            "text": str(summary)[:2800],
            "markdown": True,
        }],
    }


def _deliver_report(cache_query, cluster_id, report_date, report_type, summary):
    """Best-effort: publish the digest to SNS (email) + POST to managed
    slack-webhook and teams-webhook subscribers. Inert unless
    REPORT_DELIVERY_ENABLED is truthy. Never raises."""
    enabled = get_config("REPORT_DELIVERY_ENABLED", os.environ.get("REPORT_DELIVERY_ENABLED", "false"))
    if str(enabled).strip().lower() not in ("true", "1", "yes", "on"):
        return
    # Fleet rollup rows carry cluster_id='*'; render it as a human label in
    # every delivery payload (the '*' stays as-is in S3/DB).
    display_cid = "Fleet 전체" if cluster_id == "*" else cluster_id
    try:
        topic = os.environ.get("ALERT_TOPIC_ARN", "")
        if topic:
            boto3.client("sns").publish(
                TopicArn=topic,
                Subject=f"DBOps 리포트 · {display_cid} · {report_date}"[:100],
                Message=summary,
            )
        subs = cache_query(
            "SELECT id, protocol, endpoint FROM alert_subscribers_managed "
            "WHERE enabled = true AND protocol IN ('slack-webhook','teams-webhook')"
        )
        for s in subs or []:
            try:
                if s["protocol"] == "teams-webhook":
                    payload = _build_report_teams_card(display_cid, report_date, report_type, summary)
                else:
                    payload = _build_report_slack_blocks(display_cid, report_date, report_type, summary)
                _post_json(s["endpoint"], payload)
            except Exception as e:
                print(f"[report-gen] deliver failed for sub {s.get('id')} ({s.get('protocol')}): {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[report-gen] delivery failed for {cluster_id}: {type(e).__name__}: {e}")
