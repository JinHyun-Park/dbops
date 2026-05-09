def _tool(name, desc, props, required=None):
    return {
        "name": name,
        "description": desc,
        "inputSchema": {
            "type": "object",
            "properties": {k: {"type": v} for k, v in props.items()},
            "required": required or [],
        },
    }


def performance_schema():
    return [
        _tool("get_top_queries", "Get top-N queries sorted by total time, calls, or mean time",
              {"cluster_id": "string", "sort_by": "string", "limit": "integer"}, ["cluster_id"]),
        _tool("get_pi_metrics", "Get Performance Insights metrics (AAS, wait events) from cache",
              {"cluster_id": "string", "metric_type": "string"}, ["cluster_id"]),
        _tool("get_slow_queries", "Get slow queries exceeding threshold from cache",
              {"cluster_id": "string", "threshold_ms": "number"}, ["cluster_id"]),
        _tool("compare_periods", "Compare metrics between two time periods",
              {"cluster_id": "string", "period_a_start": "string", "period_a_end": "string",
               "period_b_start": "string", "period_b_end": "string"},
              ["cluster_id", "period_a_start", "period_a_end", "period_b_start", "period_b_end"]),
        _tool("detect_anomalies", "Detect metric anomalies using z-score against 7-day baseline",
              {"cluster_id": "string", "hours": "integer"}, ["cluster_id"]),
        _tool("detect_regressions", "Detect query regressions after a change point",
              {"cluster_id": "string", "change_point": "string"}, ["cluster_id", "change_point"]),
        _tool("forecast_capacity", "Forecast storage/connection capacity limits",
              {"cluster_id": "string", "metric": "string"}, ["cluster_id"]),
        _tool("get_performance_summary", "Get KPI summary for a time period",
              {"cluster_id": "string", "hours": "integer"}, ["cluster_id"]),
        _tool("recommend_index", "Recommend indexes based on sequential scan patterns",
              {"cluster_id": "string"}, ["cluster_id"]),
        _tool("get_vacuum_stats", "Get autovacuum stats and bloat ratio per table",
              {"cluster_id": "string"}, ["cluster_id"]),
    ]


def incident_schema():
    return [
        _tool("get_health_status", "Get cluster health status overview",
              {"cluster_id": "string"}, ["cluster_id"]),
        _tool("get_recent_events", "Get recent RDS events and alarms",
              {"cluster_id": "string", "hours": "integer"}, ["cluster_id"]),
        _tool("search_logs", "Search CloudWatch Logs via Insights",
              {"cluster_id": "string", "query": "string", "hours": "integer"}, ["cluster_id"]),
        _tool("correlate_signals", "Correlate metrics and events on timeline",
              {"cluster_id": "string", "start_time": "string", "end_time": "string"},
              ["cluster_id", "start_time", "end_time"]),
        _tool("get_incident_summary", "Get incident statistics (MTTR, frequency)",
              {"cluster_id": "string", "days": "integer"}, ["cluster_id"]),
        _tool("find_similar_incidents", "Find similar past incidents from knowledge base",
              {"cluster_id": "string", "symptoms": "string"}, ["cluster_id", "symptoms"]),
    ]


def operations_schema():
    return [
        _tool("get_schema_diff", "Compare schemas between two time points",
              {"cluster_id": "string"}, ["cluster_id"]),
        _tool("get_schema_history", "Track schema change history",
              {"cluster_id": "string", "days": "integer"}, ["cluster_id"]),
        _tool("execute_sql", "Execute SQL (SELECT auto, DDL/DML requires approval)",
              {"cluster_id": "string", "sql": "string", "approved": "boolean"}, ["cluster_id", "sql"]),
        _tool("modify_parameter", "Modify DB parameter (requires approval)",
              {"cluster_id": "string", "parameter_name": "string", "value": "string", "approved": "boolean"},
              ["cluster_id", "parameter_name", "value"]),
        _tool("modify_scaling", "Scale instance (requires approval)",
              {"cluster_id": "string", "min_capacity": "number", "max_capacity": "number", "approved": "boolean"},
              ["cluster_id"]),
        _tool("manage_maintenance", "View or modify maintenance window",
              {"cluster_id": "string", "action": "string"}, ["cluster_id"]),
        _tool("review_sql", "Pre-execution SQL review with risk assessment",
              {"cluster_id": "string", "sql": "string"}, ["cluster_id", "sql"]),
        _tool("audit_permissions", "Audit DB user permissions",
              {"cluster_id": "string"}, ["cluster_id"]),
    ]


def simulation_schema():
    return [
        _tool("check_upgrade_compatibility", "Check version upgrade compatibility",
              {"cluster_id": "string", "target_version": "string"}, ["cluster_id", "target_version"]),
        _tool("estimate_upgrade_impact", "Estimate upgrade time, downtime, and risk",
              {"cluster_id": "string", "target_version": "string"}, ["cluster_id", "target_version"]),
        _tool("generate_upgrade_plan", "Generate step-by-step upgrade plan",
              {"cluster_id": "string", "target_version": "string", "method": "string"},
              ["cluster_id", "target_version"]),
        _tool("simulate_parameter_change", "Simulate parameter change impact",
              {"cluster_id": "string", "parameter_name": "string", "new_value": "string"},
              ["cluster_id", "parameter_name", "new_value"]),
        _tool("simulate_scaling", "Simulate scaling cost-performance tradeoff",
              {"cluster_id": "string", "new_min_acu": "number", "new_max_acu": "number"}, ["cluster_id"]),
        _tool("simulate_ddl_impact", "Estimate DDL execution impact (lock time, duration)",
              {"cluster_id": "string", "ddl_sql": "string"}, ["cluster_id", "ddl_sql"]),
    ]
