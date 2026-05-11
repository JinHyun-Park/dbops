from datetime import datetime, timedelta

def collect_pi_metrics(pi_client, cache_execute, cluster_resource_id, cluster_id):
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=5)

    response = pi_client.get_resource_metrics(
        ServiceType="RDS",
        Identifier=cluster_resource_id,
        MetricQueries=[{"Metric": "db.load.avg", "GroupBy": {"Group": "db.wait_event"}}],
        StartTime=start_time, EndTime=end_time, PeriodInSeconds=60,
    )

    inserted = 0
    for metric_result in response.get("MetricList", []):
        for point in metric_result.get("DataPoints", []):
            sql = """INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions)
                     VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, :dimensions::jsonb)"""
            params = {
                "cluster_id": cluster_id, "ts": point["Timestamp"].isoformat(),
                "metric_type": "aas", "value": point.get("Value", 0.0), "dimensions": "{}",
            }
            cache_execute(sql, params)
            inserted += 1
    return {"cluster_id": cluster_id, "metrics_inserted": inserted}
