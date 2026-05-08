import aws_cdk.aws_bedrock_agentcore_alpha as ac


def _prop(type_str):
    type_map = {
        "string": ac.SchemaDefinitionType.STRING,
        "integer": ac.SchemaDefinitionType.INTEGER,
        "number": ac.SchemaDefinitionType.NUMBER,
        "boolean": ac.SchemaDefinitionType.BOOLEAN,
    }
    return ac.SchemaDefinition(type=type_map.get(type_str, ac.SchemaDefinitionType.STRING))


def _tool(name, desc, props, required=None):
    schema_props = {k: _prop(v["type"]) for k, v in props.items()}
    return ac.ToolDefinition(
        name=name,
        description=desc,
        input_schema=ac.SchemaDefinition(
            type=ac.SchemaDefinitionType.OBJECT,
            properties=schema_props,
            required=required or [],
        ),
    )


def performance_tools():
    return [
        _tool("get_top_queries", "Get top-N queries sorted by total time, calls, or mean time",
              {"cluster_id": {"type": "string"}, "sort_by": {"type": "string"}, "limit": {"type": "integer"}}, ["cluster_id"]),
        _tool("get_pi_metrics", "Get Performance Insights metrics (AAS, wait events) from cache",
              {"cluster_id": {"type": "string"}, "metric_type": {"type": "string"}}, ["cluster_id"]),
        _tool("get_slow_queries", "Get slow queries exceeding threshold from cache",
              {"cluster_id": {"type": "string"}, "threshold_ms": {"type": "number"}}, ["cluster_id"]),
        _tool("compare_periods", "Compare metrics between two time periods",
              {"cluster_id": {"type": "string"}, "period_a_start": {"type": "string"}, "period_a_end": {"type": "string"},
               "period_b_start": {"type": "string"}, "period_b_end": {"type": "string"}},
              ["cluster_id", "period_a_start", "period_a_end", "period_b_start", "period_b_end"]),
        _tool("detect_anomalies", "Detect metric anomalies using z-score against 7-day baseline",
              {"cluster_id": {"type": "string"}, "hours": {"type": "integer"}}, ["cluster_id"]),
        _tool("detect_regressions", "Detect query regressions after a change point",
              {"cluster_id": {"type": "string"}, "change_point": {"type": "string"}}, ["cluster_id", "change_point"]),
        _tool("forecast_capacity", "Forecast storage/connection capacity limits",
              {"cluster_id": {"type": "string"}, "metric": {"type": "string"}}, ["cluster_id"]),
        _tool("get_performance_summary", "Get KPI summary for a time period",
              {"cluster_id": {"type": "string"}, "hours": {"type": "integer"}}, ["cluster_id"]),
        _tool("recommend_index", "Recommend indexes based on sequential scan patterns",
              {"cluster_id": {"type": "string"}}, ["cluster_id"]),
        _tool("get_vacuum_stats", "Get autovacuum stats and bloat ratio per table",
              {"cluster_id": {"type": "string"}}, ["cluster_id"]),
    ]


def incident_tools():
    return [
        _tool("get_health_status", "Get cluster health status overview",
              {"cluster_id": {"type": "string"}}, ["cluster_id"]),
        _tool("get_recent_events", "Get recent RDS events and alarms",
              {"cluster_id": {"type": "string"}, "hours": {"type": "integer"}}, ["cluster_id"]),
        _tool("search_logs", "Search CloudWatch Logs via Insights",
              {"cluster_id": {"type": "string"}, "query": {"type": "string"}, "hours": {"type": "integer"}}, ["cluster_id"]),
        _tool("correlate_signals", "Correlate metrics and events on timeline",
              {"cluster_id": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}},
              ["cluster_id", "start_time", "end_time"]),
        _tool("get_incident_summary", "Get incident statistics (MTTR, frequency)",
              {"cluster_id": {"type": "string"}, "days": {"type": "integer"}}, ["cluster_id"]),
        _tool("find_similar_incidents", "Find similar past incidents from knowledge base",
              {"cluster_id": {"type": "string"}, "symptoms": {"type": "string"}}, ["cluster_id", "symptoms"]),
    ]


def operations_tools():
    return [
        _tool("get_schema_diff", "Compare schemas between two time points",
              {"cluster_id": {"type": "string"}}, ["cluster_id"]),
        _tool("get_schema_history", "Track schema change history",
              {"cluster_id": {"type": "string"}, "days": {"type": "integer"}}, ["cluster_id"]),
        _tool("execute_sql", "Execute SQL (SELECT auto, DDL/DML requires approval)",
              {"cluster_id": {"type": "string"}, "sql": {"type": "string"}, "approved": {"type": "boolean"}}, ["cluster_id", "sql"]),
        _tool("modify_parameter", "Modify DB parameter (requires approval)",
              {"cluster_id": {"type": "string"}, "parameter_name": {"type": "string"}, "value": {"type": "string"},
               "approved": {"type": "boolean"}}, ["cluster_id", "parameter_name", "value"]),
        _tool("modify_scaling", "Scale instance (requires approval)",
              {"cluster_id": {"type": "string"}, "min_capacity": {"type": "number"}, "max_capacity": {"type": "number"},
               "approved": {"type": "boolean"}}, ["cluster_id"]),
        _tool("manage_maintenance", "View or modify maintenance window",
              {"cluster_id": {"type": "string"}, "action": {"type": "string"}}, ["cluster_id"]),
        _tool("review_sql", "Pre-execution SQL review with risk assessment",
              {"cluster_id": {"type": "string"}, "sql": {"type": "string"}}, ["cluster_id", "sql"]),
        _tool("audit_permissions", "Audit DB user permissions",
              {"cluster_id": {"type": "string"}}, ["cluster_id"]),
    ]


def simulation_tools():
    return [
        _tool("check_upgrade_compatibility", "Check version upgrade compatibility",
              {"cluster_id": {"type": "string"}, "target_version": {"type": "string"}}, ["cluster_id", "target_version"]),
        _tool("estimate_upgrade_impact", "Estimate upgrade time, downtime, and risk",
              {"cluster_id": {"type": "string"}, "target_version": {"type": "string"}}, ["cluster_id", "target_version"]),
        _tool("generate_upgrade_plan", "Generate step-by-step upgrade plan",
              {"cluster_id": {"type": "string"}, "target_version": {"type": "string"}, "method": {"type": "string"}},
              ["cluster_id", "target_version"]),
        _tool("simulate_parameter_change", "Simulate parameter change impact",
              {"cluster_id": {"type": "string"}, "parameter_name": {"type": "string"}, "new_value": {"type": "string"}},
              ["cluster_id", "parameter_name", "new_value"]),
        _tool("simulate_scaling", "Simulate scaling cost-performance tradeoff",
              {"cluster_id": {"type": "string"}, "new_min_acu": {"type": "number"}, "new_max_acu": {"type": "number"}},
              ["cluster_id"]),
        _tool("simulate_ddl_impact", "Estimate DDL execution impact (lock time, duration)",
              {"cluster_id": {"type": "string"}, "ddl_sql": {"type": "string"}}, ["cluster_id", "ddl_sql"]),
    ]
