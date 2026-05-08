// Performance MCP Server: READ-ONLY
// Only SELECT and EXPLAIN queries allowed via explain_query tool

permit(
  principal,
  action in [
    Action::"get_top_queries",
    Action::"get_pi_metrics",
    Action::"get_slow_queries",
    Action::"compare_periods",
    Action::"detect_anomalies",
    Action::"detect_regressions",
    Action::"forecast_capacity",
    Action::"get_performance_summary"
  ],
  resource
);
