"""outcome_evaluator — open remediation cases, then judge the due ones.

EventBridge every 20 min. Public endpoints only (RDS Data API), so it lives in the
data stack like proactive_monitor / alert_evaluator.
"""
import os

import boto3

from outcome_evaluator import case_opener, evaluator


def _query(rds_data, cluster_arn, secret_arn, database, sql, params=None):
    sql_params = []
    if params:
        for k, v in params.items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            elif v is None:
                sql_params.append({"name": k, "value": {"isNull": True}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn, secretArn=secret_arn, database=database,
        sql=f"/* source=dbops-outcome-eval */ {sql}", parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [c["name"] for c in resp.get("columnMetadata", [])]
    rows = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
            else:
                row[col] = None
        rows.append(row)
    return rows


def _due_cases(q):
    return q(
        "SELECT case_id, cluster_id, symptom_class, symptom_subject, watch_metric, "
        "action_class, opened_at FROM remediation_cases "
        "WHERE status = 'open' AND evaluate_after <= NOW() LIMIT 500"
    )


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    def q(sql, params=None):
        return _query(rds_data, cluster_arn, secret_arn, database, sql, params)

    opened = case_opener.open_cases(q)
    evaluated = 0
    for case in _due_cases(q) or []:
        try:
            verdict = evaluator.evaluate_case(q, case)
            evaluator.apply_verdict(q, case, verdict)
            evaluated += 1
        except Exception as e:
            print(f"[outcome-eval] case {case.get('case_id')} failed: {type(e).__name__}: {e}")

    print(f"[outcome-eval] opened={opened} evaluated={evaluated}")
    return {"opened": opened, "evaluated": evaluated}
