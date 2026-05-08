// Incident MCP Server: READ-ONLY

permit(
  principal,
  action in [
    Action::"get_health_status",
    Action::"get_recent_events",
    Action::"search_logs",
    Action::"correlate_signals",
    Action::"get_incident_summary",
    Action::"find_similar_incidents"
  ],
  resource
);
