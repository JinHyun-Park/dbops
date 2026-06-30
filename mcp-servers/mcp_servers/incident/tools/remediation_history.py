"""MCP tool: expose the learned remediation track record to the chat agent."""


def get_remediation_history_impl(cache, cluster_id: str, symptom_class: str = "") -> dict:
    # ponytail: `where` is assembled from string literals only; user input goes in params — no injection risk.
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
    # ponytail: `recent_where` assembled from string literals only; user input only ever in recent_params.
    recent_where = "cluster_id = :cid AND status IN ('resolved','persisted')"
    recent_params: dict = {"cid": cluster_id}
    if symptom_class:
        recent_where += " AND symptom_class = :sc"
        recent_params["sc"] = symptom_class
    recent = cache.execute(
        f"SELECT symptom_class, action_class, status, evaluated_at FROM remediation_cases "
        f"WHERE {recent_where} ORDER BY evaluated_at DESC LIMIT 20", recent_params,
    ).rows
    return {"actions": actions, "recent": recent}
