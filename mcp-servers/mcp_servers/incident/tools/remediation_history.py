"""MCP tool: expose the learned remediation track record to the chat agent."""


def get_remediation_history_impl(cache, cluster_id: str, symptom_class: str = "") -> dict:
    where = "cluster_id = :cid"
    params = {"cid": cluster_id}
    if symptom_class:
        where += " AND symptom_class = :sc"
        params["sc"] = symptom_class
    actions = cache.execute(
        f"SELECT action_class, symptom_class, successes, attempts, last_outcome "
        f"FROM remediation_outcomes_agg WHERE {where} AND attempts > 0 "
        f"ORDER BY attempts DESC LIMIT 50", params,
    ).rows
    recent = cache.execute(
        "SELECT symptom_class, action_class, status, evaluated_at FROM remediation_cases "
        "WHERE cluster_id = :cid AND status IN ('resolved','persisted') "
        "ORDER BY evaluated_at DESC LIMIT 20", {"cid": cluster_id},
    ).rows
    return {"actions": actions, "recent": recent}
