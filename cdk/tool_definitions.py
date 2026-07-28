def _tool(name, desc, props, required=None, enums=None):
    """enums={"param": [...]}로 허용값을 Gateway 스키마에 실어 보낸다. 에이전트는
    이 스키마만 보므로, 여기에 값이 없으면 문서에만 있는 옛 값(예: forecast_capacity
    metric='storage_gb')을 계속 보낸다. props는 flat 유지(핸들러↔스키마 parity
    테스트가 `"name": "type"` 형태를 파싱한다)."""
    properties = {k: {"type": v} for k, v in props.items()}
    for param, values in (enums or {}).items():
        properties[param]["enum"] = values
    return {
        "name": name,
        "description": desc,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


def performance_schema():
    return [
        # 스키마는 핸들러 시그니처의 전체 파라미터를 노출해야 한다 — 누락된
        # 파라미터는 기본값으로만 동작해 에이전트 능력이 조용히 제한된다
        # (예: start_time/end_time이 없으면 "어제 14~15시 슬로우쿼리" 같은
        # 시간창 분석이 불가능). request_approval 누락 P0와 같은 패밀리.
        _tool("get_top_queries", "Get top-N queries sorted by total time, calls, or mean time; optional ISO start_time/end_time window",
              {"cluster_id": "string", "sort_by": "string", "limit": "integer",
               "start_time": "string", "end_time": "string"}, ["cluster_id"]),
        _tool("get_pi_metrics", "Get Performance Insights metrics (AAS, wait events) from cache; optional ISO start_time/end_time window",
              {"cluster_id": "string", "metric_type": "string",
               "start_time": "string", "end_time": "string"}, ["cluster_id"]),
        _tool("get_slow_queries", "Get slow queries exceeding threshold from cache; optional ISO start_time/end_time window",
              {"cluster_id": "string", "threshold_ms": "number", "limit": "integer",
               "start_time": "string", "end_time": "string"}, ["cluster_id"]),
        _tool("compare_periods", "Compare metrics between two time periods",
              {"cluster_id": "string", "period_a_start": "string", "period_a_end": "string",
               "period_b_start": "string", "period_b_end": "string", "metric_type": "string"},
              ["cluster_id", "period_a_start", "period_a_end", "period_b_start", "period_b_end"]),
        _tool("detect_anomalies", "Detect metric anomalies using z-score against 7-day baseline",
              {"cluster_id": "string", "hours": "integer", "threshold": "number"}, ["cluster_id"]),
        _tool("detect_regressions", "Detect query regressions after a change point",
              {"cluster_id": "string", "change_point": "string", "hours_before": "integer",
               "hours_after": "integer", "min_change_pct": "number"},
              ["cluster_id", "change_point"]),
        _tool("forecast_capacity",
              "Forecast when a metric reaches its limit by linear regression over the series "
              "actually collected for the cluster's engine. metric is a LOGICAL name (there is "
              "no storage_gb), and the SAME vocabulary the dashboard /capacity-forecast endpoint "
              "takes, so tool and panel agree: storage = Aurora/DocumentDB volume growth toward "
              "the 128 TiB ceiling or standalone-RDS free space depleting toward 0, connections = "
              "DatabaseConnections vs max_connections (cluster_meta, else cluster_settings, else "
              "the DocumentDB DatabaseConnectionsLimit datapoint), aas = vs instance vCPU "
              "(Serverless v2: serverlessv2_max_acu converted to vCPU), read_capacity / "
              "write_capacity = DynamoDB consumed_rcu/wcu per minute vs the latest provisioned "
              "rate x 60 (an on-demand table has no provisioned row, so grounded=false and no "
              "date), memory = ElastiCache Redis/Valkey DatabaseMemoryUsagePercentage vs 100 "
              "(Memcached does not publish it and is refused). Every metric is "
              "per-engine-family: an engine that collects no such series is refused with "
              "status=unsupported_metric, never a zero-sample forecast. status is always "
              "present: ok | limit_reached | evicting | no_data | unsupported_metric | "
              "unknown_metric | unknown_cluster. direction is up (growing toward a ceiling) or "
              "down (depleting toward 0), and usage_pct is precomputed 0-100 or null, so never "
              "divide by limit: it is legitimately 0 in the down mode. status=evicting means an "
              "LRU/TTL cache is already recycling memory, where a days-to-100% number would be "
              "meaningless; that is NOT an all-clear, the accurate signal is the "
              "elasticache_evictions_spike / elasticache_memory_pressure findings. "
              "days_until_limit is 0 with status=limit_reached when the value is ALREADY at/past "
              "the limit (urgent, observed not extrapolated), a positive int when the trend "
              "heads there, else null; no date at all when the limit cannot be grounded in the "
              "cluster's real config. approaching_limit is true when at the limit or when the "
              "ETA is within 365 days (actionable), so a distant ETA reports its date with "
              "approaching_limit=false and the note explains why. confidence and "
              "days_until_limit_range describe the FIT, not the horizon",
              {"cluster_id": "string", "metric": "string", "days_lookback": "integer"},
              ["cluster_id"],
              enums={"metric": ["storage", "connections", "aas", "read_capacity",
                                "write_capacity", "memory"]}),
        _tool("get_performance_summary", "Get KPI summary for a time period",
              {"cluster_id": "string", "hours": "integer"}, ["cluster_id"]),
        _tool("recommend_index",
              "Emit CREATE INDEX CONCURRENTLY DDL by parsing heavy queries (table + "
              "composite columns from WHERE/JOIN/ORDER BY), corroborated by table_stats "
              "seq scans; read-only advice validated via EXPLAIN and the approval flow",
              {"cluster_id": "string", "min_seq_scan_ratio": "number"}, ["cluster_id"]),
        _tool("get_vacuum_stats", "Get autovacuum stats and bloat ratio per table",
              {"cluster_id": "string"}, ["cluster_id"]),
        _tool("explain_plan",
              "Run EXPLAIN on a SELECT and return a structured plan analysis "
              "(seq scans, bad row estimates, disk spills, expensive nodes); "
              "analyze=true actually runs the query for real timings",
              {"cluster_id": "string", "sql": "string", "analyze": "boolean"},
              ["cluster_id", "sql"]),
    ]


def incident_schema():
    return [
        _tool("get_health_status", "Get cluster health status overview",
              {"cluster_id": "string"}, ["cluster_id"]),
        _tool("get_recent_events", "Get recent RDS events and alarms; optional event_type filter",
              {"cluster_id": "string", "hours": "integer", "event_type": "string"}, ["cluster_id"]),
        _tool("search_logs",
              "Search DB logs via CloudWatch Logs Insights. Defaults to the Aurora cluster "
              "error log; pass log_group explicitly for another DB log, e.g. "
              "/aws/docdb/{cluster_id}/profiler (DocumentDB profiler output, enabled by "
              "set_docdb_profiler) or /aws/rds/instance/{id}/slowquery. Only "
              "/aws/rds/cluster/, /aws/rds/instance/ and /aws/docdb/ groups are allowed",
              {"cluster_id": "string", "query": "string", "hours": "integer",
               "log_group": "string"}, ["cluster_id"]),
        _tool("correlate_signals", "Correlate metrics and events on timeline",
              {"cluster_id": "string", "start_time": "string", "end_time": "string"},
              ["cluster_id", "start_time", "end_time"]),
        _tool("diagnose_root_cause",
              "Rank candidate root causes (schema changes, events, locks, metric spikes, "
              "slow queries) around an incident by proximity and severity",
              {"cluster_id": "string", "around_time": "string", "window_minutes": "integer"},
              ["cluster_id"]),
        _tool("get_incident_summary", "Get incident statistics (MTTR, frequency)",
              {"cluster_id": "string", "days": "integer"}, ["cluster_id"]),
        _tool("find_similar_incidents", "Find similar past incidents from knowledge base",
              {"cluster_id": "string", "symptoms": "string"}, ["cluster_id", "symptoms"]),
        _tool(
            "get_maintenance_findings",
            "Get the latest maintenance/health findings (issues + recommendations) "
            "for a cluster of ANY engine (Aurora, DocumentDB, DynamoDB)",
            {"cluster_id": "string"},
            ["cluster_id"],
        ),
        _tool(
            "get_remediation_history",
            "Get the learned remediation track record for a cluster — aggregated "
            "success/attempt counts per action class (remediation_outcomes_agg) and "
            "the 20 most recent resolved/persisted cases (remediation_cases). "
            "Use this to advise DBAs on which remediations have worked before.",
            {"cluster_id": "string", "symptom_class": "string"},
            ["cluster_id"],
        ),
    ]


def operations_schema():
    return [
        _tool("get_schema_diff", "Compare schemas between two snapshots (ISO timestamps); omit both for latest-vs-previous",
              {"cluster_id": "string", "snapshot_a": "string", "snapshot_b": "string"},
              ["cluster_id"]),
        _tool("get_schema_history", "Track schema change history",
              {"cluster_id": "string", "days": "integer"}, ["cluster_id"]),
        # force는 DROP/TRUNCATE 차단 해제용 — 노출해도 인간 승인 게이트
        # (approved+approval_id 검증)는 그대로 통과해야 하므로 안전 모델
        # 위반이 아니다. 미노출 시 차단 메시지가 force를 안내하는데 에이전트가
        # 전달할 방법이 없는 자기모순이 된다.
        _tool("execute_sql", "Execute SQL (SELECT auto, DDL/DML requires approval; DROP/TRUNCATE additionally requires force=true)",
              {"cluster_id": "string", "sql": "string", "approved": "boolean",
               "approval_id": "string", "force": "boolean"}, ["cluster_id", "sql"]),
        _tool("request_approval",
              "Create a DBA approval request in the Approval Center when a write "
              "tool returned approval_required. Returns approval_id + /approvals "
              "deep link; after the DBA approves, re-issue the write tool with "
              "approved=true AND that approval_id",
              {"cluster_id": "string", "action_type": "string",
               "action_details": "object", "requested_by": "string"},
              ["cluster_id", "action_type", "action_details"]),
        _tool("modify_parameter", "Modify DB parameter (requires approval)",
              {"cluster_id": "string", "parameter_name": "string", "value": "string", "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "parameter_name", "value"]),
        _tool("modify_scaling", "Scale instance (requires approval)",
              {"cluster_id": "string", "min_capacity": "number", "max_capacity": "number", "approved": "boolean", "approval_id": "string"},
              ["cluster_id"]),
        # 쓰기 툴 3종 모두 approved/approval_id를 스키마에 노출해야 한다 —
        # 핸들러·가드가 완비여도 스키마에 없으면 에이전트가 승인 후 재실행을
        # 못 해 승인 루프가 dead-end가 된다 (request_approval 누락 P0와 동일
        # 패밀리, 시나리오 테스트로 적발).
        _tool("manage_maintenance", "View or modify maintenance window (modify requires approval)",
              {"cluster_id": "string", "action": "string", "window": "string",
               "approved": "boolean", "approval_id": "string"}, ["cluster_id"]),
        _tool("create_snapshot", "Create a manual cluster snapshot (backup); requires approval",
              {"cluster_id": "string", "snapshot_id": "string",
               "approved": "boolean", "approval_id": "string"}, ["cluster_id"]),
        _tool("restore_cluster",
              "Restore a snapshot or point-in-time into a NEW cluster (high risk); requires approval",
              {"cluster_id": "string", "new_cluster_id": "string", "mode": "string",
               "snapshot_id": "string", "restore_to_time": "string", "use_latest": "boolean",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "new_cluster_id"]),
        # Aurora custom cluster endpoints (P2-⑤). Approval-gated writes; each
        # exposes approved/approval_id and returns cli_preview (the exact aws rds
        # command) for the approval card. Every handler param must appear here or
        # the impl<->schema parity test fails.
        _tool("create_custom_endpoint",
              "Aurora only: create a custom DB cluster endpoint (READER or ANY) routing a subset of readers; requires approval. Returns cli_preview",
              {"cluster_id": "string", "endpoint_identifier": "string", "endpoint_type": "string",
               "static_members": "array", "excluded_members": "array",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "endpoint_identifier"]),
        _tool("delete_custom_endpoint",
              "Aurora only: delete a CUSTOM DB cluster endpoint (built-in writer/reader are protected); requires approval. Returns cli_preview",
              {"cluster_id": "string", "endpoint_identifier": "string",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "endpoint_identifier"]),
        _tool("modify_custom_endpoint",
              "Aurora only: change StaticMembers or ExcludedMembers of a CUSTOM DB cluster endpoint (mutually exclusive, one required); requires approval. Returns cli_preview",
              {"cluster_id": "string", "endpoint_identifier": "string",
               "static_members": "array", "excluded_members": "array",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "endpoint_identifier"]),
        # Reader buffer-cache prewarm (P2-④). Aurora PostgreSQL only; approval-
        # gated. Exposes approved/approval_id and returns a step plan as
        # cli_preview. Every handler param must appear here or the impl<->schema
        # parity test fails.
        _tool("prewarm_reader",
              "Aurora PostgreSQL only: prewarm a COLD reader instance's buffer pool before it takes traffic (optional custom-endpoint exclude/include, pg_prewarm top-N relations, pg_buffercache before/after); requires approval. Returns a step plan as cli_preview",
              {"cluster_id": "string", "reader_instance_id": "string", "endpoint_identifier": "string",
               "top_n": "integer", "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "reader_instance_id"]),
        # Reader scale-out/scale-in (N-③). Aurora only (instance-level, PG+MySQL);
        # approval-gated. Every handler param must appear here or the impl<->schema
        # parity test fails.
        _tool("add_reader_instance",
              "Aurora only (scale-out): add a new READER instance to expand read capacity; new_instance_id required; instance_class defaults to the writer's class (Serverless v2 -> db.serverless); requires approval",
              {"cluster_id": "string", "new_instance_id": "string", "instance_class": "string",
               "availability_zone": "string", "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "new_instance_id"]),
        _tool("remove_reader_instance",
              "Aurora only (scale-in): remove a READER instance; the writer and the cluster's last instance are protected; irreversible; requires approval",
              {"cluster_id": "string", "instance_id": "string",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "instance_id"]),
        # Reader scale-out + auto-warmup (N-④ Phase 1). Approval #1 of a
        # semi-automatic, two-approval flow: creates a reader AND auto-queues a
        # prewarm approval that becomes DBA-visible once the reader is available.
        # Every handler param must appear here or the impl<->schema parity test fails.
        _tool("scale_out_with_warmup",
              "Aurora only (scale-out + auto-warmup): add a READER instance AND auto-queue its buffer-pool prewarm (semi-automatic, two approvals — this is approval #1; the prewarm auto-appears in the Approval Center once the reader is available); new_instance_id required; instance_class defaults to the writer's class; requires approval",
              {"cluster_id": "string", "new_instance_id": "string", "instance_class": "string",
               "endpoint_identifier": "string", "top_n": "integer",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "new_instance_id"]),
        # AZ scale-out runbook planner (P2-⑥). READ-ONLY — no approved/approval_id
        # (it plans; the /scaleout-az API mints add_reader_instance approvals). Not
        # in request_approval's enum. Every handler param must appear here or the
        # impl<->schema parity test fails.
        _tool("plan_az_scaleout",
              "Aurora only (read-only): plan a preemptive AZ scale-out — N reader instances spread round-robin over the cluster's healthy AZs, EXCLUDING one chosen AZ, each with a concrete instance_class + AZ + unique id. Creates nothing",
              {"cluster_id": "string", "exclude_az": "string", "count": "integer",
               "instance_class": "string"},
              ["cluster_id"]),
        # NoSQL write/remediation (multi-engine #P3.6 Group C). The 3 DynamoDB
        # tools + the 2 DocDB Mongo writes (set_docdb_profiler, create_docdb_index)
        # all ship with both their handler impl and this schema entry so the
        # impl<->schema parity test stays green. Each exposes approved/approval_id
        # (and force where applicable) so the agent can complete the approval
        # round-trip.
        _tool("modify_dynamodb_capacity",
              "DynamoDB only: change provisioned RCU/WCU and/or switch billing mode (Provisioned<->On-Demand); requires approval. Blocks tables with any GSI; rejects RCU/WCU < 1",
              {"cluster_id": "string", "billing_mode": "string", "rcu": "integer",
               "wcu": "integer", "approved": "boolean", "approval_id": "string"},
              ["cluster_id"]),
        _tool("modify_dynamodb_ttl",
              "DynamoDB only: enable or disable an attribute TTL (update_time_to_live); requires approval; idempotent",
              {"cluster_id": "string", "attribute": "string", "enabled": "boolean",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "attribute"]),
        _tool("enable_dynamodb_pitr",
              "DynamoDB only: turn Point-in-Time Recovery on/off (update_continuous_backups); requires approval. DISABLING additionally requires force=true",
              {"cluster_id": "string", "enabled": "boolean", "force": "boolean",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id"]),
        _tool("set_docdb_profiler",
              "DocumentDB only: enable or disable the CLUSTER-wide profiler via the custom cluster parameter group (profiler, profiler_threshold_ms >= 50, profiler_sampling_rate 0.0-1.0) plus the profiler CloudWatch Logs export (per-cluster log group /aws/docdb/{cluster_id}/profiler). The parameter group is SHARED, so every cluster attached to it inherits the change, and the approval is bound to the resolved group. You MUST copy parameter_group from the approval_required response into request_approval's action_details, otherwise the approval payload hash will never match and the approved re-run is refused. Refuses a default.* parameter group; requires approval",
              {"cluster_id": "string", "enabled": "boolean", "threshold_ms": "integer",
               "sampling_rate": "number", "approved": "boolean", "approval_id": "string"},
              ["cluster_id"]),
        _tool("create_docdb_index",
              "DocumentDB only: create a (compound) index via the Mongo protocol with background=true; keys is an ORDERED list of [field, direction] pairs and name is required; requires approval; idempotent",
              {"cluster_id": "string", "db": "string", "collection": "string",
               "keys": "array", "name": "string", "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "db", "collection", "keys", "name"]),
        _tool("elasticache_live_read",
              "ElastiCache only: live Redis/Valkey/Memcached deep-read — INFO, SLOWLOG, CLIENT LIST, MEMORY STATS (Redis) or stats (Memcached). Read-only; no mutation.",
              {"cluster_id": "string", "sections": "array"},
              ["cluster_id"]),
        _tool("modify_elasticache_node_type",
              "ElastiCache only: scale the node type (modify_replication_group). Approval-gated write.",
              {"cluster_id": "string", "node_type": "string",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "node_type"]),
        _tool("create_elasticache_snapshot",
              "ElastiCache (Redis/Valkey) only: create a backup snapshot. Approval-gated write.",
              {"cluster_id": "string", "snapshot_name": "string",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "snapshot_name"]),
        _tool("reboot_elasticache",
              "ElastiCache only: reboot the primary cache cluster node (reboot_cache_cluster). Approval-gated write.",
              {"cluster_id": "string", "approved": "boolean", "approval_id": "string"},
              ["cluster_id"]),
        _tool("test_elasticache_failover",
              "ElastiCache only: test failover for a replication group node group (requires a replica). Approval-gated write.",
              {"cluster_id": "string", "node_group_id": "string",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id"]),
        # Standalone RDS instance write tools (R-3). Non-Aurora MySQL/SQL Server
        # (rds_instance family); each is approval-gated and exposes approved/
        # approval_id. Every handler kwarg must appear here or the impl<->schema
        # parity test fails.
        _tool("reboot_rds_instance",
              "Standalone RDS instance only (non-Aurora): reboot the DB instance; Aurora cluster members are refused; requires approval",
              {"cluster_id": "string", "approved": "boolean", "approval_id": "string"},
              ["cluster_id"]),
        _tool("create_rds_snapshot",
              "Standalone RDS instance only (non-Aurora): create a manual DB instance snapshot; snapshot_id default is resolved+bound at approval time; requires approval",
              {"cluster_id": "string", "snapshot_id": "string",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id"]),
        _tool("modify_rds_instance_class",
              "Standalone RDS instance only (non-Aurora): change the DB instance compute class (modify_db_instance, ApplyImmediately); current_class is bound at approval time and drift is refused; requires approval",
              {"cluster_id": "string", "target_class": "string", "current_class": "string",
               "approved": "boolean", "approval_id": "string"},
              ["cluster_id", "target_class"]),
        _tool("review_sql", "Pre-execution SQL review with risk assessment",
              {"cluster_id": "string", "sql": "string"}, ["cluster_id", "sql"]),
        _tool("audit_permissions", "Audit DB user permissions",
              {"cluster_id": "string", "engine": "string"}, ["cluster_id"]),
        _tool(
            "query_activity_audit",
            "Search write history + approval log for compliance / retro questions "
            "('who changed max_connections last week?')",
            {
                "cluster_id": "string",
                "actor": "string",
                "action_type": "string",
                "days": "integer",
            },
            [],
        ),
        _tool(
            "get_runbook",
            "Fetch a saved runbook by id or fuzzy title/tag, returning its SQL "
            "steps to run via execute_sql (approval-gated); read-only",
            {"runbook_id": "string", "query": "string"},
            [],
        ),
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
        _tool("simulate_scaling",
              "Simulate scaling cost with real AWS pricing — Serverless v2 ACU range (new_min_acu/new_max_acu) OR provisioned instance resize (new_instance_class)",
              {"cluster_id": "string", "new_min_acu": "number", "new_max_acu": "number", "new_instance_class": "string"}, ["cluster_id"]),
        _tool("simulate_ddl_impact", "Estimate DDL execution impact (lock time, duration)",
              {"cluster_id": "string", "ddl_sql": "string"}, ["cluster_id", "ddl_sql"]),
        _tool("simulate_dynamodb_capacity_cost",
              "DynamoDB only: compare Provisioned vs On-Demand monthly cost from the table's actual consumed capacity, priced with the real AWS Price List API",
              {"cluster_id": "string", "headroom": "number", "window_hours": "number"}, ["cluster_id"]),
        _tool("simulate_elasticache_node_resize",
              "ElastiCache only: estimate the monthly cost of resizing an ElastiCache cluster node type / count using the AWS Price List API. Read-only, no approval required.",
              {"cluster_id": "string", "new_node_type": "string", "new_node_count": "integer"}, ["cluster_id"]),
        _tool("simulate_rds_instance_rightsizing",
              "RDS instance (MySQL/SQL Server) only: CW-driven right-sizing with real Price List cost delta (compute + storage/IOPS + SQL Server license). Read-only, no approval.",
              {"cluster_id": "string", "window_hours": "number", "headroom": "number", "new_instance_class": "string"}, ["cluster_id"]),
    ]
