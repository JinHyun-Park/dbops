"""Judge open cases and fold the verdict into the learned aggregate."""

K = 3  # robust band half-width in IQRs (matches the anomaly detector's z>3)
EVAL_LOOKBACK_MIN = 60  # recent-window for metric recovery


def _first(rows, key, default=None):
    return rows[0].get(key) if rows else default


def evaluate_case(query, case) -> str:
    if case.get("watch_metric"):
        return _evaluate_metric(query, case)
    return _evaluate_finding(query, case)


def _evaluate_metric(query, case) -> str:
    recent = query(
        # ponytail: EVAL_LOOKBACK_MIN is a module constant (not user input) — f-string is injection-safe
        "SELECT AVG(value) AS v FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = :m "
        f"AND ts > NOW() - INTERVAL '{EVAL_LOOKBACK_MIN} minutes' "
        "AND (dimensions IS NULL OR NOT jsonb_exists(dimensions, 'instance'))",
        {"cid": case["cluster_id"], "m": case["watch_metric"]},
    )
    v = _first(recent, "v")
    if v is None:
        return "inconclusive"
    base = query(
        "SELECT median, iqr FROM metric_baselines "
        "WHERE cluster_id = :cid AND metric_type = :m "
        "AND hour_of_week = (EXTRACT(DOW FROM NOW())::int * 24 + EXTRACT(HOUR FROM NOW())::int)",
        {"cid": case["cluster_id"], "m": case["watch_metric"]},
    )
    if not base or _first(base, "median") is None:
        return "inconclusive"
    median, iqr = float(base[0]["median"]), float(base[0]["iqr"])
    lo, hi = median - K * iqr, median + K * iqr
    return "resolved" if lo <= float(v) <= hi else "persisted"


def _evaluate_finding(query, case) -> str:
    parts = case["symptom_class"].split(":", 1)
    if len(parts) < 2:
        # F2: malformed/legacy symptom_class (no colon) — can't extract check_type
        return "inconclusive"
    recurred = query(
        "SELECT COUNT(*) AS recurred FROM cluster_health_findings "
        "WHERE cluster_id = :cid AND check_type = :ct AND subject = :subj "
        "AND snapshot_time > :since",
        {"cid": case["cluster_id"], "ct": parts[1],
         "subj": case["symptom_subject"], "since": case["opened_at"]},
    )
    if int(_first(recurred, "recurred", 0) or 0) > 0:
        return "persisted"
    # False-resolved guard: only trust "cleared" if the collector actually ran —
    # i.e. the cluster produced ANY finding row since the case opened.
    produced = query(
        "SELECT COUNT(*) AS produced FROM cluster_health_findings "
        "WHERE cluster_id = :cid AND snapshot_time > :since",
        {"cid": case["cluster_id"], "since": case["opened_at"]},
    )
    return "resolved" if int(_first(produced, "produced", 0) or 0) > 0 else "inconclusive"


def apply_verdict(query, case, verdict) -> None:
    if verdict == "inconclusive":
        # No signal — don't move the aggregate; just close the case.
        query(
            "UPDATE remediation_cases SET status = :st, evaluated_at = NOW() WHERE case_id = :id",
            {"st": verdict, "id": case["case_id"]},
        )
        return
    succ_inc = 1 if verdict == "resolved" else 0
    # F1: agg upserts FIRST, case UPDATE last.
    # ponytail: best-effort, not transactional — a failure between the two agg writes
    # leaves the case status='open' so the next run retries (may double-count by 1);
    # full atomicity via an RDS Data API transaction is the upgrade path.
    for cid in (case["cluster_id"], "*"):
        query(
            "INSERT INTO remediation_outcomes_agg "
            "(cluster_id, symptom_class, action_class, attempts, successes, last_outcome, "
            " last_success_at, updated_at) "
            "VALUES (:cluster_id, :symptom_class, :action_class, 1, :succ_inc, :verdict, "
            " CASE WHEN :succ_inc = 1 THEN NOW() ELSE NULL END, NOW()) "
            "ON CONFLICT (cluster_id, symptom_class, action_class) DO UPDATE SET "
            " attempts = remediation_outcomes_agg.attempts + 1, "
            " successes = remediation_outcomes_agg.successes + :succ_inc, "
            " last_outcome = :verdict, "
            " last_success_at = CASE WHEN :succ_inc = 1 THEN NOW() "
            "                        ELSE remediation_outcomes_agg.last_success_at END, "
            " updated_at = NOW()",
            {"cluster_id": cid, "symptom_class": case["symptom_class"],
             "action_class": case["action_class"], "succ_inc": succ_inc, "verdict": verdict},
        )
    query(
        "UPDATE remediation_cases SET status = :st, evaluated_at = NOW() WHERE case_id = :id",
        {"st": verdict, "id": case["case_id"]},
    )
