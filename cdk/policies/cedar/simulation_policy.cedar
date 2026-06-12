// Simulation MCP Server: READ-ONLY (what-if estimates, no mutation)
// Every simulation tool only reads cache/RDS metadata and computes an estimate —
// none execute a change. Aurora-only at the tool level (handler engine guard).

permit(
  principal,
  action in [
    Action::"check_upgrade_compatibility",
    Action::"estimate_upgrade_impact",
    Action::"generate_upgrade_plan",
    Action::"simulate_parameter_change",
    Action::"simulate_scaling",
    Action::"simulate_ddl_impact",
    Action::"simulate_dynamodb_capacity_cost"
  ],
  resource
);
