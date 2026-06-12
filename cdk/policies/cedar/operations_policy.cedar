// Operations MCP Server: MIXED (read auto / write approval required)

// Read-only tools: always permitted
permit(
  principal,
  action in [
    Action::"get_schema_diff",
    Action::"get_schema_history",
    Action::"review_sql",
    Action::"audit_permissions",
    // request_approval registers a DBA approval row (no DB change); the audit
    // search and runbook fetch are read-only. Explore found these absent → the
    // Gateway default-DENY was silently blocking them. Added to the read block.
    Action::"request_approval",
    Action::"query_activity_audit",
    Action::"get_runbook"
  ],
  resource
);

// SELECT queries: auto-permitted
permit(
  principal,
  action == Action::"execute_sql",
  resource
) when {
  context.input.sql.toUpper().startsWith("SELECT") ||
  context.input.sql.toUpper().startsWith("EXPLAIN") ||
  context.input.sql.toUpper().startsWith("SHOW") ||
  context.input.sql.toUpper().startsWith("DESCRIBE")
};

// Write tools: require approved flag
permit(
  principal,
  action in [
    Action::"execute_sql",
    Action::"modify_parameter",
    Action::"modify_scaling",
    Action::"manage_maintenance",
    // Explore found create_snapshot/restore_cluster absent from the write
    // block → silent Gateway DENY despite the handler + guard being complete.
    Action::"create_snapshot",
    Action::"restore_cluster",
    // NoSQL write/remediation (multi-engine #P3.6 Group C). All five are
    // approval-gated exactly like modify_scaling. The two DocDB Mongo writes
    // ship stage 2, but their Cedar actions are added now so the policy stays
    // coherent with the approval enum + guard projections.
    Action::"modify_dynamodb_capacity",
    Action::"modify_dynamodb_ttl",
    Action::"enable_dynamodb_pitr",
    Action::"set_docdb_profiler",
    Action::"create_docdb_index"
  ],
  resource
) when {
  context.input.approved == true
};

// Block dangerous SQL without force flag
forbid(
  principal,
  action == Action::"execute_sql",
  resource
) when {
  context.input.sql.toUpper().contains("DROP") ||
  context.input.sql.toUpper().contains("TRUNCATE")
} unless {
  context.input.force == true
};

// DISABLING PITR is a data-protection degradation — forbid unless force==true.
// The force flag is also hashed into the approval payload, so the DBA approves
// the forceful (disable) variant specifically (review fix #7).
forbid(
  principal,
  action == Action::"enable_dynamodb_pitr",
  resource
) when {
  context.input.enabled == false
} unless {
  context.input.force == true
};
