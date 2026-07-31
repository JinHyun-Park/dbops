"""set_docdb_profiler: approval-gated Amazon DocumentDB profiler change via the
CONTROL PLANE (cluster parameter group + CloudWatch Logs export).

Managed Amazon DocumentDB does NOT support enabling the profiler over the Mongo
wire protocol: there is no `db.command("profile", ...)` surface, and profiler
output never lands in a `system.profile` collection (system.* collections are
unsupported). Per
https://docs.aws.amazon.com/documentdb/latest/devguide/profiling.html enabling
the profiler is a three-step, IAM-authorized change:

  1. In a CUSTOM cluster parameter group set `profiler` (enabled|disabled),
     `profiler_threshold_ms` (50..INT_MAX) and `profiler_sampling_rate`
     (0.0..1.0). A `default.*` group CANNOT be modified.
  2. The cluster must USE that custom group. This tool does NOT create or attach
     one: a cluster still on a default group is refused with that reason stated
     (creating + attaching a group is a heavier, separate operation).
  3. Export the `profiler` log type to CloudWatch Logs via modify_db_cluster.
     Without step 3 the parameter is on but nothing is delivered. The log group
     is PER CLUSTER: /aws/docdb/{cluster_id}/profiler (AWS docs, "Accessing your
     Amazon DocumentDB profiler logs"), not a single shared /aws/docdb/profiler.

The profiler log group IS readable from DBOps: `search_logs` allows the
/aws/docdb/ prefix, so the operator queries it with
log_group="/aws/docdb/{cluster_id}/profiler". It must be passed explicitly,
because search_logs defaults to the cluster ERROR log
(/aws/rds/cluster/{id}/error), and the dashboard log panel still shows only that
error log. The tool's `note` says exactly this. Indexing profiler records into
the cache (so they show up without an explicit query) is E-1 work.

Safety model (unchanged from the previous Mongo-protocol version):
  - FAIL-CLOSED engine gate in the handler (docdb_write capability). A None
    family never reaches this impl.
  - Approval-gated 3-state flow (approval_required -> verify_approval -> execute).
    verify_approval is consumed EXACTLY ONCE and is payload-hash bound to the
    effective {enabled, threshold_ms, sampling_rate} AND to the resolved
    parameter_group. Binding the group matters because a cluster parameter group
    is SHARED: if the cluster were re-pointed at a different group between
    approval and execute, an unbound approval would modify a group the DBA never
    reviewed, along with every other cluster attached to it. With the group in
    the hash that reassignment fails verification (fail-closed).
  - NEVER raises, and NEVER returns raw exception text: static Korean reason +
    module logger.

There are no Mongo credentials any more (the old `mongo_write_secret_arn`
dependency is gone): the calls run as the operations Lambda's IAM role, cross
account via the registry's spoke role. No TOCTOU re-read either: every call
writes ABSOLUTE parameter values, so there is no read-modify-write window a
re-read could protect (unlike the DynamoDB capacity tool).
"""

import logging

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster
from mcp_servers.shared.managed_tag_preflight import (
    aurora_cluster_tag_warning,
    cluster_parameter_group_tag_warning,
)

logger = logging.getLogger(__name__)

# DocumentDB parameter bounds (AWS docs): profiler_threshold_ms is [50-INT_MAX],
# profiler_sampling_rate is [0.0-1.0]. The profiler params are dynamic, so
# ApplyMethod=immediate is valid (a static param would require pending-reboot).
MIN_THRESHOLD_MS = 50
MAX_THRESHOLD_MS = 2147483647  # INT_MAX, the upper end AWS documents
DEFAULT_THRESHOLD_MS = 100
DEFAULT_SAMPLING_RATE = 1.0


def validate_profiler_params(threshold_ms=None, sampling_rate=None):
    """Coerce + range-check the two profiler knobs against the range the tool
    description advertises. Returns (threshold_int, rate_float, error_reason);
    error_reason is "" when both are in range. None means "not supplied" and
    takes the tool default.

    request_approval calls this too: the registration path mints the
    payload_hash the write is bound to, so a value the write would refuse must
    never reach the Approval Center in the first place. One definition for both
    paths, so the two can't drift."""
    if threshold_ms is None:
        threshold_ms = DEFAULT_THRESHOLD_MS
    if sampling_rate is None:
        sampling_rate = DEFAULT_SAMPLING_RATE
    try:
        threshold_i = int(threshold_ms)
    except (TypeError, ValueError):
        return 0, 0.0, "threshold_ms는 정수여야 합니다 (밀리초)."
    if not MIN_THRESHOLD_MS <= threshold_i <= MAX_THRESHOLD_MS:
        return 0, 0.0, (
            f"threshold_ms는 {MIN_THRESHOLD_MS} 이상 {MAX_THRESHOLD_MS} 이하여야 합니다 "
            "(DocumentDB 허용 범위: 50~INT_MAX)."
        )
    try:
        rate_f = float(sampling_rate)
    except (TypeError, ValueError):
        return 0, 0.0, "sampling_rate는 0.0~1.0 사이의 실수여야 합니다."
    if not 0.0 <= rate_f <= 1.0:
        return 0, 0.0, "sampling_rate는 0.0~1.0 범위여야 합니다."
    return threshold_i, rate_f, ""


def profiler_log_group(cluster_id: str) -> str:
    """CloudWatch Logs group the profiler delivers to, PER CLUSTER.

    AWS docs (Profiling Amazon DocumentDB operations, "Accessing your Amazon
    DocumentDB profiler logs"): the group is /aws/docdb/{yourClusterName}/profiler.
    There is no single shared /aws/docdb/profiler group, so pointing an operator
    at that name sends them to a log group that does not exist."""
    return f"/aws/docdb/{cluster_id}/profiler"


def set_docdb_profiler_impl(
    cache,
    cluster_id: str,
    enabled: bool = True,
    threshold_ms: int = DEFAULT_THRESHOLD_MS,
    sampling_rate: float = DEFAULT_SAMPLING_RATE,
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    """Turn the DocumentDB profiler on/off for `cluster_id` (cluster-wide: the
    profiler applies to every database and instance). Approval-gated; the
    approval binds {enabled, threshold_ms, sampling_rate, parameter_group}.
    Never raises."""
    # --- validate BEFORE any AWS call (no partial writes) ---
    # `enabled` must be a real JSON boolean. A string flag is REFUSED instead of
    # coerced: an ambiguous value must not reach the approval hash on either side
    # (the approval projection coerces action_details with the shared as_bool, so
    # a registered "false" binds the DISABLE payload, and the tool then only
    # ever executes a real bool, which as_bool maps to itself).
    if not isinstance(enabled, bool):
        return {
            "status": "error",
            "reason": (
                "enabled는 JSON boolean(true/false)이어야 합니다. 문자열 플래그는 "
                "승인된 값과 실제 실행 값이 어긋날 수 있어 거부합니다."
            ),
            "cluster_id": cluster_id,
        }
    enabled_b = enabled

    threshold_i, rate_f, param_error = validate_profiler_params(threshold_ms, sampling_rate)
    if param_error:
        return {
            "status": "error",
            "reason": param_error,
            "cluster_id": cluster_id,
        }

    try:
        client = client_for_cluster(cluster_id, "docdb")
    except Exception:
        logger.warning("docdb client init failed for %s", cluster_id, exc_info=True)
        return {
            "status": "error",
            "reason": "DocumentDB 제어 플레인 클라이언트를 만들 수 없습니다 (자세한 원인은 서버 로그를 확인하세요).",
            "cluster_id": cluster_id,
        }

    # --- step 1/2: which parameter group does the cluster actually use? ---
    try:
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = (resp.get("DBClusters") or [{}])[0]
    except Exception:
        logger.warning("describe_db_clusters failed for %s", cluster_id, exc_info=True)
        return {
            "status": "lookup_failed",
            "reason": "클러스터 정보를 조회할 수 없습니다 (자세한 원인은 서버 로그를 확인하세요).",
            "cluster_id": cluster_id,
        }

    pg_name = (cluster.get("DBClusterParameterGroup") or "").strip()
    if not pg_name:
        return {
            "status": "no_parameter_group",
            "reason": "클러스터의 파라미터 그룹을 확인할 수 없습니다.",
            "cluster_id": cluster_id,
        }
    # AWS-managed default groups are immutable, so the profiler simply cannot be
    # turned on without first creating a custom group. Say that instead of
    # failing with an API error.
    #
    # Why the name prefix is the test: RDS/DocDB expose no IsDefault flag on a
    # parameter group, and AWS names every managed default group exactly
    # `default.<family>` (default.docdb5.0, ...). Group names are unique per
    # account+region and the managed default already holds that name, so a custom
    # group cannot occupy it. Names are stored lowercase, hence .lower(). This is
    # a pre-check to avoid burning the approval on an impossible change, not the
    # only line of defense: if it ever missed, ModifyDBClusterParameterGroup
    # itself refuses a default group and we return a static error below
    # (fail-closed, nothing is written).
    if pg_name.lower().startswith("default."):
        return {
            "status": "default_group_refused",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "reason": (
                f"이 클러스터는 AWS 기본 클러스터 파라미터 그룹('{pg_name}')을 사용합니다. "
                "기본 그룹은 수정할 수 없어 프로파일러를 켤 수 없습니다. 커스텀 클러스터 "
                "파라미터 그룹을 만들어 클러스터에 연결한 뒤 다시 시도하세요 "
                "(그룹 생성/연결은 이 도구의 범위가 아닙니다)."
            ),
        }

    log_export_on = "profiler" in (cluster.get("EnabledCloudwatchLogsExports") or [])

    warnings = []
    if enabled_b and threshold_i < 100:
        warnings.append(
            "threshold_ms를 100ms 미만으로 낮추면 처리량이 높은 클러스터에서 성능 문제가 "
            "발생할 수 있습니다 (AWS 권장: 500ms에서 시작해 점진적으로 낮추기)."
        )
    warnings.append(
        f"파라미터 그룹 '{pg_name}'을 공유하는 다른 클러스터에도 같은 변경이 적용됩니다."
    )

    if not approved:
        card = {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "enabled": enabled_b,
            "threshold_ms": threshold_i,
            "sampling_rate": rate_f,
            "current_log_export": log_export_on,
            "warnings": warnings,
        }
        # This tool writes TWO tag-gated resources: the cluster parameter group
        # (modify_db_cluster_parameter_group) and the cluster itself (the
        # profiler log-export via modify_db_cluster). Either one missing the tag
        # denies its half, so both are reported. Cross-account only, WARNING never
        # a refusal: see managed_tag_preflight.
        tag_warnings = [
            w for w in (
                cluster_parameter_group_tag_warning(client, cluster_id, pg_name),
                aurora_cluster_tag_warning(
                    client, cluster_id, action="rds:ModifyDBCluster"),
            ) if w
        ]
        if tag_warnings:
            card["warning"] = "\n".join(tag_warnings)
        return card

    # `pg_name` was resolved from the LIVE cluster a few lines above, so passing
    # it here is what makes the group a bound target: if the cluster was
    # re-pointed at another parameter group after the DBA approved, the hash no
    # longer matches and the guard refuses instead of writing to a group nobody
    # reviewed (and to every cluster sharing it).
    guard = verify_approval(
        approval_id,
        cluster_id,
        "set_docdb_profiler",
        payload={
            "enabled": enabled_b,
            "threshold_ms": threshold_i,
            "sampling_rate": rate_f,
            "parameter_group": pg_name,
        },
    )
    if not guard.get("ok"):
        return {
            "status": "approval_denied",
            "reason": guard.get("reason", "approval guard rejected the request"),
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "hint": (
                f"현재 클러스터가 사용하는 파라미터 그룹은 '{pg_name}'입니다. 승인 시점과 "
                "다른 그룹이면 승인이 무효가 됩니다 (승인은 그룹까지 고정됩니다). "
                "그룹이 바뀌었다면 다시 요청해 새로 승인받으세요."
            ),
        }

    # --- step 1: the parameter-group write (absolute values, idempotent) ---
    params = [{
        "ParameterName": "profiler",
        "ParameterValue": "enabled" if enabled_b else "disabled",
        "ApplyMethod": "immediate",
    }]
    if enabled_b:
        params.append({
            "ParameterName": "profiler_threshold_ms",
            "ParameterValue": str(threshold_i),
            "ApplyMethod": "immediate",
        })
        params.append({
            "ParameterName": "profiler_sampling_rate",
            "ParameterValue": str(rate_f),
            "ApplyMethod": "immediate",
        })

    try:
        client.modify_db_cluster_parameter_group(
            DBClusterParameterGroupName=pg_name, Parameters=params
        )
    except Exception:
        logger.warning(
            "modify_db_cluster_parameter_group failed for %s (group=%s)",
            cluster_id, pg_name, exc_info=True,
        )
        return {
            "status": "error",
            "reason": "클러스터 파라미터 그룹 수정에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요).",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
        }

    # --- step 3: profiler log export. Skip when already in the wanted state:
    # re-enabling an already-enabled log type is an API error.
    log_cfg = None
    if enabled_b and not log_export_on:
        log_cfg = {"EnableLogTypes": ["profiler"]}
    elif not enabled_b and log_export_on:
        log_cfg = {"DisableLogTypes": ["profiler"]}
    if log_cfg:
        try:
            client.modify_db_cluster(
                DBClusterIdentifier=cluster_id, CloudwatchLogsExportConfiguration=log_cfg
            )
            log_export_on = enabled_b
        except Exception:
            logger.warning(
                "modify_db_cluster log export failed for %s (%s)",
                cluster_id, log_cfg, exc_info=True,
            )
            return {
                "status": "partial",
                "cluster_id": cluster_id,
                "parameter_group": pg_name,
                "enabled": enabled_b,
                "log_export": log_export_on,
                "reason": (
                    "파라미터 그룹은 변경했지만 profiler 로그 내보내기 설정 변경에 "
                    "실패했습니다. 로그 내보내기가 켜져 있지 않으면 프로파일러 출력이 "
                    "CloudWatch Logs로 전달되지 않습니다 (자세한 원인은 서버 로그를 확인하세요)."
                ),
            }

    result = {
        "status": "modified",
        "cluster_id": cluster_id,
        "parameter_group": pg_name,
        "profiler": "enabled" if enabled_b else "disabled",
        "log_export": log_export_on,
        "log_group": profiler_log_group(cluster_id),
        "note": (
            f"파라미터 그룹 '{pg_name}'에 profiler={'enabled' if enabled_b else 'disabled'}를 "
            "ApplyMethod=immediate로 적용했습니다 (반영까지 몇 분 걸릴 수 있습니다). "
            f"프로파일러 출력은 CloudWatch Logs 로그 그룹 {profiler_log_group(cluster_id)}로 "
            "전송됩니다 (로그 그룹은 첫 레코드가 생긴 뒤에 나타납니다). "
            f"조회는 search_logs에 log_group=\"{profiler_log_group(cluster_id)}\"를 "
            "넘기면 됩니다 (기본값은 클러스터 error 로그이므로 명시해야 합니다). "
            "DocumentDB에는 system.profile 컬렉션이 없으므로 Mongo 셸로는 조회할 수 없습니다."
        ),
    }
    # Report only what was actually written: disabling touches the `profiler`
    # switch alone, so echoing a threshold/sampling value there would be a claim
    # about parameters this call never set.
    if enabled_b:
        result["threshold_ms"] = threshold_i
        result["sampling_rate"] = rate_f
    return result
