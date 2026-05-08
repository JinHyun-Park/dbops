// Operations MCP Server: MIXED (read auto / write approval required)

// Read-only tools: always permitted
permit(
  principal,
  action in [
    Action::"get_schema_diff",
    Action::"get_schema_history",
    Action::"review_sql",
    Action::"audit_permissions"
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
    Action::"manage_maintenance"
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
