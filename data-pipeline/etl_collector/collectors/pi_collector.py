import json
from datetime import datetime, timedelta

PI_METRIC_QUERIES = [
    {"Metric": "db.load.avg", "GroupBy": {"Group": "db.wait_event"}, "metric_type": "aas"},
    {"Metric": "os.cpuUtilization.total.avg", "metric_type": "cpu"},
    {"Metric": "db.SQL.tup_returned.avg", "metric_type": "tup_returned"},
    {"Metric": "db.Transactions.xact_commit.avg", "metric_type": "xact_commit"},
    {"Metric": "db.SQL.numbackends.avg", "metric_type": "connections"},
    {"Metric": "db.Cache.blks_hit.avg", "metric_type": "cache_hit"},
    {"Metric": "db.Checkpoint.checkpoints_timed.avg", "metric_type": "checkpoint"},
    {"Metric": "os.memory.free.avg", "metric_type": "mem_free"},
    {"Metric": "os.diskIO.rdsdev.readIOsPS.avg", "metric_type": "read_iops"},
    {"Metric": "os.diskIO.rdsdev.writeIOsPS.avg", "metric_type": "write_iops"},
    {"Metric": "os.network.rx.avg", "metric_type": "net_rx"},
    {"Metric": "os.network.tx.avg", "metric_type": "net_tx"},
]

# RDS instance engines (non-Aurora): only db.load.avg is universally supported
# across MySQL AND SQL Server PI (SQL Server exposes NO os.* counters and no
# PG-shaped db.* counters; one unknown metric fails the whole batched
# GetResourceMetrics call — live-verified 2026-07-22). Engine-specific PI
# depth lands in R-2 (MySQL) / R-4 (SQL Server).
PI_METRICS_RDS_INSTANCE = [
    {"Metric": "db.load.avg", "GroupBy": {"Group": "db.wait_event"}, "metric_type": "aas"},
]


INSERT_SQL = (
    "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
    "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, :dimensions::jsonb) "
    "ON CONFLICT DO NOTHING"
)


def collect_pi_metrics(pi_client, cache_execute, resource_id, cluster_id, metrics=None):
    metrics = metrics if metrics is not None else PI_METRIC_QUERIES
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=10)

    # PI supports up to 15 MetricQueries per call; batch all in one request.
    queries = []
    for idx, q in enumerate(metrics):
        mq = {"Metric": q["Metric"]}
        if "GroupBy" in q:
            mq["GroupBy"] = q["GroupBy"]
        queries.append(mq)

    try:
        response = pi_client.get_resource_metrics(
            ServiceType="RDS",
            Identifier=resource_id,
            MetricQueries=queries,
            StartTime=start_time,
            EndTime=end_time,
            PeriodInSeconds=60,
        )
    except Exception as e:
        return {"cluster_id": cluster_id, "metrics_inserted": 0, "errors": [str(e)]}

    inserted = 0
    metric_type_by_name = {q["Metric"]: q["metric_type"] for q in metrics}

    for metric_result in response.get("MetricList", []):
        key = metric_result.get("Key", {})
        metric_name = key.get("Metric", "")
        metric_type = metric_type_by_name.get(metric_name)
        if not metric_type:
            continue
        dims = key.get("Dimensions") or {}
        dim_json = json.dumps(dims, sort_keys=True) if dims else "{}"

        for point in metric_result.get("DataPoints", []):
            if "Value" not in point:
                continue
            cache_execute(INSERT_SQL, {
                "cluster_id": cluster_id,
                "ts": point["Timestamp"].isoformat(),
                "metric_type": metric_type,
                "value": float(point["Value"]),
                "dimensions": dim_json,
            })
            inserted += 1

    return {"cluster_id": cluster_id, "metrics_inserted": inserted, "errors": []}
