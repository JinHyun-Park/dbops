"""Synthetic data generator for demo mode (P1.4).

Seeds ~24h of realistic-looking metrics/queries/findings/etc. into the
cache DB so a fresh evaluator can experience the full dashboard before
connecting a real cluster. Idempotent — re-running for the same
cluster_id wipes prior demo rows and re-inserts relative to "now".
"""

import math
import random
from datetime import datetime, timedelta, timezone

SAMPLE_CLUSTER_ID = "sample-cluster"
SAMPLE_ENGINE = "aurora-postgresql"

# (baseline, daily_amplitude, noise_std, spike_hour, spike_value)
# spike_hour is hour-of-day for the synthetic incident at ~14:00 local time.
METRIC_PROFILES = {
    "cpu":            (40,   15,   5,    14,   88),
    "aas":            (0.6,  0.4,  0.15, 14,   4.2),
    # Canonical total-connections metric (matches CloudWatch DatabaseConnections
    # from cw_collector). Spikes to ~150 (75% of the seeded max_connections=200)
    # during the synthetic 14:00 incident so connection-storm demos have signal.
    "db_connections": (45,   20,   6,    14,   150),
    "conn_active":    (12,   6,    2,    14,   45),
    "conn_idle":      (28,   8,    4,    None, None),
    "read_iops":      (350,  200,  80,   9,    2400),
    "write_iops":     (180,  80,   40,   14,   1100),
    "xact_commit":    (120,  60,   20,   14,   780),
    "tup_returned":   (28_000, 12_000, 4_000, 14, 95_000),
    "storage_bytes":  (25_000_000_000, 500_000_000, 10_000_000, None, None),
    "replica_lag_ms": (40,   20,   15,   14,   1200),
    "deadlocks":      (0,    0,    0.3,  14,   2),
}

# (query_text, calls_24h, mean_time_ms, hit_ratio_pct)
QUERY_PATTERNS = [
    ("SELECT * FROM orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT 50",
     12_000,  4.2,  92),
    ("SELECT id, total FROM invoices WHERE status = 'open' AND created_at > $1",
     8_400,   3.1,  96),
    ("UPDATE sessions SET last_seen_at = NOW() WHERE id = $1",
     45_000,  0.8,  99),
    ("SELECT COUNT(*) FROM events WHERE created_at > NOW() - INTERVAL '1 day' GROUP BY type",
     180,     870.0, 12),
    ("SELECT u.email, COUNT(o.id) FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.email",
     20,      4_300.0, 8),
    ("INSERT INTO audit_log (actor, action, payload) VALUES ($1, $2, $3)",
     32_000,  0.6,  100),
    ("SELECT * FROM products WHERE name ILIKE $1 LIMIT 20",
     6_800,   14.2, 78),
    ("DELETE FROM tmp_uploads WHERE created_at < NOW() - INTERVAL '1 hour'",
     12,      142.0, 60),
    ("SELECT id FROM jobs WHERE state = 'queued' ORDER BY priority DESC, created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
     54_000,  0.9,  98),
    ("WITH ranked AS (SELECT id, score, RANK() OVER (PARTITION BY tenant ORDER BY score DESC) r FROM matches) SELECT * FROM ranked WHERE r <= 10",
     900,     280.0, 24),
]

EXTENSIONS = [
    ("pg_stat_statements", "1.10"),
    ("pgcrypto", "1.3"),
    ("pg_trgm", "1.6"),
    ("uuid-ossp", "1.1"),
    ("plpgsql", "1.0"),
    # pg_repack intentionally absent → triggers extension_missing finding
]

SETTINGS = [
    ("max_connections", "200", ""),
    ("shared_buffers", "16384", "8kB"),
    ("work_mem", "4096", "kB"),
    ("log_min_duration_statement", "-1", "ms"),  # misconfigured
    ("log_lock_waits", "off", ""),                # misconfigured
    ("log_temp_files", "-1", "kB"),               # misconfigured
    ("autovacuum_naptime", "60", "s"),
]

# (schema, table, live, dead, seq, idx, total_bytes, table_bytes, index_bytes)
TABLES = [
    ("public",    "orders",      4_120_000,    92_000,    8_400,   142_000_000, 32_000_000_000,  22_000_000_000, 10_000_000_000),
    ("public",    "users",         240_000,     2_000,    1_200,    18_400_000,    480_000_000,     320_000_000,    160_000_000),
    ("public",    "audit_log",  28_000_000,   412_000,  198_000,     3_400_000, 84_000_000_000,  71_000_000_000, 13_000_000_000),
    ("public",    "sessions",    1_400_000,   320_000,    8_400,    58_000_000,  5_200_000_000,   3_800_000_000,  1_400_000_000),
    ("public",    "products",       85_000,     1_400,      940,     8_200_000,    320_000_000,     240_000_000,     80_000_000),
    ("public",    "invoices",      820_000,    18_000,    4_100,    24_000_000, 12_000_000_000,   9_400_000_000,  2_600_000_000),
    ("analytics", "events",    142_000_000, 8_400_000,  920_000,     1_400_000, 920_000_000_000, 720_000_000_000, 200_000_000_000),
    ("analytics", "rollups",     4_200_000,    38_000,    1_200,    42_000_000, 18_000_000_000,  14_000_000_000,  4_000_000_000),
]

# Synthetic operational findings — varied severity so dashboard demonstrates filtering.
FINDINGS = [
    ("txid_age", "warning", "public.audit_log",
     "age=612,000,000", "< 200,000,000",
     "Heaviest tables show transaction-id age > 200M. Run VACUUM (FREEZE) on public.audit_log before reaching wraparound.",
     {"schema": "public", "table": "audit_log", "age": 612_000_000}),
    ("dead_tuples", "warning", "analytics.events",
     "8400000", "5000000",
     "8.4M dead tuples on analytics.events (~6% of live rows). Consider lowering autovacuum_vacuum_scale_factor for this table.",
     {"n_dead_tup": 8_400_000, "n_live_tup": 142_000_000}),
    ("table_bloat", "info", "public.sessions",
     "23%", "20%",
     "public.sessions has ~23% estimated bloat. pg_repack or partition + drop is the usual fix.",
     {"bloat_pct": 23.0}),
    ("index_unused", "info", "public.orders.idx_orders_legacy_status",
     "0 scans / 30d", "any usage",
     "Index never used over the last 30 days. Confirm with pg_stat_user_indexes and drop after a release.",
     {"idx_scan": 0, "size_mb": 412}),
    ("extension_missing", "info", "pg_repack",
     "not installed", "installed",
     "pg_repack is the standard online table-rewrite extension for Aurora PostgreSQL. Install via SHARED_PRELOAD_LIBRARIES + CREATE EXTENSION.",
     {"extension": "pg_repack"}),
    ("setting_misconfigured", "warning", "log_min_duration_statement",
     "-1 ms (disabled)", ">= 1000 ms",
     "Slow-query logging is off. Set log_min_duration_statement to 1000 to capture queries above 1s.",
     {"current": "-1", "recommended": ">=1000"}),
    ("setting_misconfigured", "warning", "log_lock_waits",
     "off", "on",
     "Lock waits are not logged. Turn log_lock_waits on so contention shows up in the slow-query log.",
     {"current": "off", "recommended": "on"}),
    # P3.3.2 demos — Serverless v2 over-provisioned ceiling + Compute Savings Plan
    # opportunity. Surfaces the new cost check_types in the Maintenance Health panel.
    ("cost_serverless_max_too_high", "info", "Serverless v2 (max 32.0 ACU)",
     "7d p95 CPU 38.2% / max 88.0%", "< 40% p95 CPU → max ACU likely overprovisioned",
     "Max ACU is 32.0 but observed p95 CPU is only 38.2%. Lower max ACU to ~16.0 for the same throughput at a fraction of the burst-cost exposure. (Serverless v2 bills per ACU-hour at peak.)",
     {"current_min_acu": 0.5, "current_max_acu": 32.0, "suggested_max_acu": 16.0, "p95_cpu": 38.2, "max_cpu": 88.0}),
    ("cost_savings_plan_opportunity", "info", "Account-level Compute Savings Plan",
     "~$42.30/mo savings projected", "> $10/mo savings → worth committing",
     "Commit $0.18/hr Compute Savings Plan (1-year, no upfront) — projected ~$42.30/mo savings. Cost Explorer → Savings Plans → Recommendations confirms the exact hourly commit.",
     {"estimated_monthly_savings_usd": 42.30, "hourly_commitment_usd": 0.18, "term_years": 1, "payment_option": "NO_UPFRONT", "lookback_days": 30}),
]


def _exec(rds_data, arn, secret, db, sql, params=None):
    """Run a single SQL statement via RDS Data API."""
    return rds_data.execute_statement(
        resourceArn=arn,
        secretArn=secret,
        database=db,
        sql=f"/* source=dbops-seeder */ {sql}",
        parameters=params or [],
    )


def _ts_param(name, dt):
    # RDS Data API's TIMESTAMP typeHint rejects timezone suffix (e.g. "+00:00");
    # drop tzinfo so we send "YYYY-MM-DD HH:MM:SS" — Aurora interprets it as UTC
    # by default and TIMESTAMPTZ columns coerce correctly.
    naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
    return {"name": name, "value": {"stringValue": naive.isoformat(sep=" ", timespec="seconds")}, "typeHint": "TIMESTAMP"}


def _str(name, val):
    return {"name": name, "value": {"stringValue": str(val)}}


def _num(name, val):
    return {"name": name, "value": {"doubleValue": float(val)}}


def _long(name, val):
    return {"name": name, "value": {"longValue": int(val)}}


def _wipe_demo_rows(rds_data, arn, secret, db, cluster_id):
    """Delete prior demo rows so re-seeding is clean."""
    for tbl in (
        "metric_snapshots", "query_stats", "blocking_locks",
        "cluster_health_findings", "cluster_extensions", "cluster_settings",
        "table_stats", "long_running_queries",
    ):
        _exec(rds_data, arn, secret, db,
              f"DELETE FROM {tbl} WHERE cluster_id = :cid",
              [_str("cid", cluster_id)])


def _seed_cluster_meta(rds_data, arn, secret, db, cluster_id):
    # The demo cluster is a Serverless v2 with a deliberately over-wide
    # max ACU (32) so the cost_serverless_max_too_high finding can fire on
    # the seeded CPU profile (low avg/p95). This gives the user a concrete
    # example of every cost check type without needing real data.
    _exec(rds_data, arn, secret, db, """
        INSERT INTO cluster_meta
            (cluster_id, account_id, region, engine, engine_version,
             instance_class, status, endpoint, max_connections, storage_size_gb,
             engine_mode, serverlessv2_min_acu, serverlessv2_max_acu, updated_at)
        VALUES (:cid, '000000000000', 'ap-northeast-2', :engine, '15.5',
                'db.serverless', 'available',
                'sample-cluster.cluster-demo.ap-northeast-2.rds.amazonaws.com',
                200, 4250,
                'serverless', 0.5, 32.0, NOW())
        ON CONFLICT (cluster_id) DO UPDATE
            SET status = 'available',
                engine_mode = EXCLUDED.engine_mode,
                serverlessv2_min_acu = EXCLUDED.serverlessv2_min_acu,
                serverlessv2_max_acu = EXCLUDED.serverlessv2_max_acu,
                updated_at = NOW()
    """, [_str("cid", cluster_id), _str("engine", SAMPLE_ENGINE)])


def _metric_value(profile, hour, minute, rng):
    base, amp, noise, spike_h, spike_v = profile
    radians = 2 * math.pi * ((hour + minute / 60.0) / 24.0)
    val = base + amp * math.sin(radians) + rng.gauss(0, noise)
    if spike_h is not None and hour == spike_h and 10 <= minute <= 45:
        val = max(val, spike_v)
    return max(0.0, val)


def _seed_metrics(rds_data, arn, secret, db, cluster_id, now, rng):
    """24h × 12 snapshots per metric (5-minute resolution). Chunked bulk insert."""
    rows = []
    for hours_ago in range(24, 0, -1):
        bucket_start = now - timedelta(hours=hours_ago)
        for minute in range(0, 60, 5):
            ts = bucket_start + timedelta(minutes=minute)
            for metric_name, profile in METRIC_PROFILES.items():
                rows.append((ts, metric_name, _metric_value(profile, bucket_start.hour, minute, rng)))

    CHUNK = 60  # 60 × 3 params + cid ≈ 181 params, well under the RDS Data API limit.
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        values_sql = ", ".join(
            f"(:cid, :t{j}, :m{j}, :v{j}, '{{}}'::jsonb)"
            for j in range(len(chunk))
        )
        params = [_str("cid", cluster_id)]
        for j, (ts, m, v) in enumerate(chunk):
            params.append(_ts_param(f"t{j}", ts))
            params.append(_str(f"m{j}", m))
            params.append(_num(f"v{j}", v))
        _exec(rds_data, arn, secret, db,
              f"INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) VALUES {values_sql}",
              params)
    return len(rows)


def _seed_query_stats(rds_data, arn, secret, db, cluster_id, now, rng):
    """5 snapshots over the last 25 minutes, 10 query patterns per snapshot."""
    inserted = 0
    for minutes_ago in (25, 20, 15, 10, 5):
        snap_ts = now - timedelta(minutes=minutes_ago)
        for idx, (text, calls, mean_ms, hit_pct) in enumerate(QUERY_PATTERNS):
            # Vary across snapshots so dashboards show motion.
            jitter = rng.uniform(0.9, 1.1)
            calls_at = int(calls * (minutes_ago / 30.0) * jitter)
            mean_at = mean_ms * jitter
            total_ms = calls_at * mean_at
            rows = int(calls_at * rng.uniform(0.6, 1.4))
            blks_hit = int(calls_at * hit_pct / 100.0 * 8)
            blks_read = int(calls_at * (1 - hit_pct / 100.0) * 8)
            query_hash = f"demo-{idx:02d}-{minutes_ago:02d}"
            _exec(rds_data, arn, secret, db, """
                INSERT INTO query_stats
                    (cluster_id, snapshot_time, query_hash, query_text,
                     calls, total_time_ms, mean_time_ms, rows_returned,
                     shared_blks_hit, shared_blks_read)
                VALUES (:cid, :t, :h, :q, :calls, :total, :mean, :rows, :hit, :read)
            """, [
                _str("cid", cluster_id), _ts_param("t", snap_ts),
                _str("h", query_hash), _str("q", text),
                _long("calls", calls_at), _num("total", total_ms),
                _num("mean", mean_at), _long("rows", rows),
                _long("hit", blks_hit), _long("read", blks_read),
            ])
            inserted += 1
    return inserted


def _seed_blocking_locks(rds_data, arn, secret, db, cluster_id, now):
    """A 3-PID blocking chain (1 root holder, 2 waiters)."""
    snap = now - timedelta(seconds=30)
    chain = [
        (18421, "app_writer", 19102, "app_reader",
         "UPDATE orders SET status='shipped' WHERE id IN (SELECT id FROM pending_shipments)",
         "SELECT * FROM orders WHERE user_id = $1 FOR UPDATE",
         "transactionid", "ShareLock", "ExclusiveLock", "public.orders", 38.2),
        (18421, "app_writer", 19104, "analytics_etl",
         "UPDATE orders SET status='shipped' WHERE id IN (SELECT id FROM pending_shipments)",
         "SELECT count(*) FROM orders WHERE created_at > NOW() - INTERVAL '5 minutes'",
         "relation", "AccessShareLock", "RowExclusiveLock", "public.orders", 12.4),
    ]
    for (blocking_pid, blocking_user, blocked_pid, blocked_user,
         blocking_q, blocked_q, locktype, blocked_mode, blocking_mode,
         relation, dur) in chain:
        _exec(rds_data, arn, secret, db, """
            INSERT INTO blocking_locks
                (cluster_id, snapshot_time, blocked_pid, blocked_user,
                 blocking_pid, blocking_user, blocked_query, blocking_query,
                 locktype, blocked_mode, blocking_mode, relation, blocked_duration_sec)
            VALUES (:cid, :t, :bpid, :buser, :gpid, :guser, :bq, :gq,
                    :lt, :bm, :gm, :rel, :dur)
        """, [
            _str("cid", cluster_id), _ts_param("t", snap),
            _long("bpid", blocked_pid), _str("buser", blocked_user),
            _long("gpid", blocking_pid), _str("guser", blocking_user),
            _str("bq", blocked_q), _str("gq", blocking_q),
            _str("lt", locktype), _str("bm", blocked_mode),
            _str("gm", blocking_mode), _str("rel", relation),
            _num("dur", dur),
        ])
    return len(chain)


def _seed_findings(rds_data, arn, secret, db, cluster_id, now):
    snap = now - timedelta(seconds=10)
    import json as _json
    for check_type, severity, subject, value_str, threshold_str, recommendation, details in FINDINGS:
        _exec(rds_data, arn, secret, db, """
            INSERT INTO cluster_health_findings
                (cluster_id, snapshot_time, check_type, severity, subject,
                 value_str, threshold_str, recommendation, details)
            VALUES (:cid, :t, :ct, :sev, :subj, :val, :thr, :rec, :det::jsonb)
        """, [
            _str("cid", cluster_id), _ts_param("t", snap),
            _str("ct", check_type), _str("sev", severity),
            _str("subj", subject), _str("val", value_str),
            _str("thr", threshold_str), _str("rec", recommendation),
            _str("det", _json.dumps(details)),
        ])
    return len(FINDINGS)


def _seed_extensions(rds_data, arn, secret, db, cluster_id, now):
    for name, version in EXTENSIONS:
        _exec(rds_data, arn, secret, db, """
            INSERT INTO cluster_extensions (cluster_id, extname, extversion, updated_at)
            VALUES (:cid, :n, :v, NOW())
            ON CONFLICT (cluster_id, extname) DO UPDATE SET extversion = EXCLUDED.extversion, updated_at = NOW()
        """, [_str("cid", cluster_id), _str("n", name), _str("v", version)])
    return len(EXTENSIONS)


def _seed_settings(rds_data, arn, secret, db, cluster_id):
    for name, value, unit in SETTINGS:
        _exec(rds_data, arn, secret, db, """
            INSERT INTO cluster_settings (cluster_id, name, value, unit, updated_at)
            VALUES (:cid, :n, :v, :u, NOW())
            ON CONFLICT (cluster_id, name) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, [_str("cid", cluster_id), _str("n", name), _str("v", value), _str("u", unit)])
    return len(SETTINGS)


def _seed_table_stats(rds_data, arn, secret, db, cluster_id, now):
    snap = now - timedelta(seconds=20)
    for schema, name, live, dead, seq, idx, total, tbl, idxb in TABLES:
        _exec(rds_data, arn, secret, db, """
            INSERT INTO table_stats
                (cluster_id, snapshot_time, schema_name, table_name,
                 n_live_tup, n_dead_tup, seq_scan, idx_scan,
                 seq_tup_read, idx_tup_fetch, last_vacuum, last_analyze,
                 total_bytes, table_bytes, index_bytes)
            VALUES (:cid, :t, :sch, :tbl, :live, :dead, :seq, :idx,
                    :seqr, :idxf, NOW() - INTERVAL '4 hours', NOW() - INTERVAL '2 hours',
                    :total, :tblb, :idxb)
        """, [
            _str("cid", cluster_id), _ts_param("t", snap),
            _str("sch", schema), _str("tbl", name),
            _long("live", live), _long("dead", dead),
            _long("seq", seq), _long("idx", idx),
            _long("seqr", seq * 1000), _long("idxf", idx * 50),
            _long("total", total), _long("tblb", tbl), _long("idxb", idxb),
        ])
    return len(TABLES)


def _seed_long_running(rds_data, arn, secret, db, cluster_id, now):
    snap = now - timedelta(seconds=5)
    long_running = [
        (29183, "analytics_etl", "active", 142.0, 142.0,
         "SELECT u.email, COUNT(o.id) FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.email",
         "IO", "DataFileRead", "10.0.2.181"),
        (29201, "app_writer",   "active", 38.5,  38.5,
         "UPDATE orders SET status='shipped' WHERE id IN (SELECT id FROM pending_shipments)",
         "Lock", "transactionid", "10.0.2.92"),
    ]
    for pid, user, state, dur, xact_dur, q, wtype, wevent, addr in long_running:
        _exec(rds_data, arn, secret, db, """
            INSERT INTO long_running_queries
                (cluster_id, snapshot_time, pid, username, state, duration_sec,
                 xact_duration_sec, query_text, wait_event_type, wait_event, client_addr)
            VALUES (:cid, :t, :pid, :u, :s, :d, :xd, :q, :wt, :we, :ca)
        """, [
            _str("cid", cluster_id), _ts_param("t", snap),
            _long("pid", pid), _str("u", user), _str("s", state),
            _num("d", dur), _num("xd", xact_dur),
            _str("q", q), _str("wt", wtype), _str("we", wevent), _str("ca", addr),
        ])
    return len(long_running)


def seed_demo_data(rds_data, cluster_arn, secret_arn, db_name, cluster_id):
    """Top-level entry point. Returns row counts per table."""
    rng = random.Random(f"dbops-demo-{cluster_id}")
    now = datetime.now(timezone.utc)

    _seed_cluster_meta(rds_data, cluster_arn, secret_arn, db_name, cluster_id)
    _wipe_demo_rows(rds_data, cluster_arn, secret_arn, db_name, cluster_id)

    return {
        "metric_snapshots": _seed_metrics(rds_data, cluster_arn, secret_arn, db_name, cluster_id, now, rng),
        "query_stats":      _seed_query_stats(rds_data, cluster_arn, secret_arn, db_name, cluster_id, now, rng),
        "blocking_locks":   _seed_blocking_locks(rds_data, cluster_arn, secret_arn, db_name, cluster_id, now),
        "health_findings":  _seed_findings(rds_data, cluster_arn, secret_arn, db_name, cluster_id, now),
        "extensions":       _seed_extensions(rds_data, cluster_arn, secret_arn, db_name, cluster_id, now),
        "settings":         _seed_settings(rds_data, cluster_arn, secret_arn, db_name, cluster_id),
        "table_stats":      _seed_table_stats(rds_data, cluster_arn, secret_arn, db_name, cluster_id, now),
        "long_running":     _seed_long_running(rds_data, cluster_arn, secret_arn, db_name, cluster_id, now),
    }


def cleanup_demo_data(rds_data, cluster_arn, secret_arn, db_name, cluster_id):
    """Remove all demo-seeded rows for a cluster_id. Used when a demo cluster is deleted."""
    _wipe_demo_rows(rds_data, cluster_arn, secret_arn, db_name, cluster_id)
    _exec(rds_data, cluster_arn, secret_arn, db_name,
          "DELETE FROM cluster_meta WHERE cluster_id = :cid",
          [_str("cid", cluster_id)])
