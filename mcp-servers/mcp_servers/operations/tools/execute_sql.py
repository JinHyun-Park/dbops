import base64
import json
import os
import re

import boto3

from mcp_servers.shared import mysql_direct
from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster
from mcp_servers.shared.engine_family import CAPABILITIES
from mcp_servers.shared.engine_family import engine_family as _engine_family

# SQL read-only/side-effect classification lives in shared/sql_safety so this
# tool and explain_plan's analyze=True gate can't drift apart. Re-exported names
# (SAFE_PATTERNS etc.) keep the existing import surface stable.
from mcp_servers.shared.sql_safety import (  # noqa: F401  (re-exported for tests)
    DANGEROUS_PATTERNS,
    SAFE_PATTERNS,
    SIDE_EFFECTING_PATTERNS,
)
from mcp_servers.shared.sql_safety import is_multi_statement as _is_multi_statement
from mcp_servers.shared.sql_safety import strip_sql_literals as _strip_literals


def _decode_array(arr: dict):
    """Decode an RDS Data API ArrayValue into a Python list (incl. nesting)."""
    for key in ("stringValues", "longValues", "doubleValues", "booleanValues"):
        if key in arr:
            return list(arr[key])
    if "arrayValues" in arr:
        return [_decode_array(a) for a in arr["arrayValues"]]
    return []


def _decode_field(field: dict):
    """Decode one RDS Data API Field into a Python value. Handles the FULL set,
    not just the four scalar types: explicit SQL NULL (isNull), bytea
    (blobValue → base64 string), and arrays (arrayValue). NUMERIC/DECIMAL come
    back as stringValue from the Data API, so exact precision is preserved by
    keeping the string. The previous decoder collapsed NULL/blob/array — and
    any unrecognized field — to None, silently losing or misrepresenting data
    in diagnostics and audit output."""
    if field.get("isNull"):
        return None
    for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if typ in field:
            return field[typ]
    if "blobValue" in field:
        blob = field["blobValue"]
        if isinstance(blob, (bytes, bytearray)):
            return base64.b64encode(bytes(blob)).decode("ascii")
        return str(blob)
    if "arrayValue" in field:
        return _decode_array(field["arrayValue"])
    return None


_CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE", "")


def _lookup_cluster(cluster_id: str) -> dict:
    """Resolve cluster_arn / secret_arn / db_name from the DynamoDB clusters
    registry. Returns {} if not found or if the table is not configured."""
    if not cluster_id:
        return {}
    if not _CLUSTERS_TABLE_NAME:
        return {}
    try:
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(_CLUSTERS_TABLE_NAME)
        resp = table.get_item(Key={"cluster_id": cluster_id})
        return resp.get("Item") or {}
    except Exception as e:
        print(f"[execute_sql] cluster lookup failed for {cluster_id}: {e}")
        return {}


def execute_sql_impl(
    cache: CacheClient,
    cluster_id: str,
    sql: str,
    approved: bool = False,
    force: bool = False,
    approval_id: str = "",
) -> dict:
    # Classify on a literal/comment-stripped copy so keyword matching reflects
    # SQL STRUCTURE, not data. The statement actually executed is still the
    # original `sql`.
    sanitized = _strip_literals(sql)
    sql_upper = sanitized.strip().upper()
    has_safe_prefix = any(re.match(p, sql_upper) for p in SAFE_PATTERNS)
    is_side_effecting = any(re.search(p, sql_upper) for p in SIDE_EFFECTING_PATTERNS)
    is_multi = _is_multi_statement(sanitized)
    is_dangerous = any(re.search(p, sql_upper) for p in DANGEROUS_PATTERNS)

    # A statement is read-only "safe" ONLY if it both looks like a read AND
    # carries no side-effecting construct AND is a single statement. A SELECT
    # that calls pg_terminate_backend(), an EXPLAIN ANALYZE (which executes the
    # plan), or a stacked "SELECT 1; UPDATE ..." all fail this and must be
    # approved like any other write.
    is_safe = has_safe_prefix and not is_side_effecting and not is_multi

    if is_dangerous and not force:
        return {"status": "blocked", "reason": "Dangerous SQL (DROP/TRUNCATE/DELETE) requires force=true", "sql": sql}

    if not is_safe and not approved:
        reason = "Non-SELECT SQL requires DBA approval"
        if has_safe_prefix and is_side_effecting:
            reason = (
                "SQL looks read-only but contains a side-effecting/state-changing "
                "construct (e.g. EXPLAIN ANALYZE, SELECT INTO, a locking clause, or "
                "a function like pg_terminate_backend) — DBA approval required"
            )
        elif has_safe_prefix and is_multi:
            reason = "Multiple SQL statements are not allowed on the read path — DBA approval required"
        return {"status": "approval_required", "reason": reason, "sql": sql}

    # Server-side approval enforcement: a write tool that claims approved=true
    # must back it up with a verifiable approval_id. The guard refuses
    # mismatched cluster, stale/replayed approvals, and unapproved rows.
    if not is_safe and approved:
        guard = verify_approval(
            approval_id, cluster_id, "execute_sql", payload={"sql": sql}
        )
        if not guard.get("ok"):
            return {
                "status": "approval_denied",
                "reason": guard.get("reason", "approval guard rejected the request"),
                "sql": sql,
            }

    # Resolve target cluster ARN/Secret from the DynamoDB clusters registry.
    # Falls back to env-var TARGET_* for legacy single-cluster deployments.
    cluster = _lookup_cluster(cluster_id)

    # resp carries the RDS-Data-API-shaped result. The direct-TCP branch below
    # fills it for rds_instance MySQL; it stays None to fall through to the
    # Aurora RDS Data API path. Both feed the SAME decode block at the end.
    resp = None

    # Engine-family guard: non-relational engines (DynamoDB, DocumentDB) do not
    # support the RDS Data API SQL path. Return a clear signal so the agent can
    # tell the user this resource type isn't supported in Phase 1 chat diagnostics
    # rather than failing with a confusing "no_target" or rds-data error.
    # Only applies to a real registry dict — legacy env-var TARGET_* deployments
    # (where _lookup_cluster yields no dict) fall straight through to the env path.
    if isinstance(cluster, dict) and cluster:
        fam = cluster.get("engine_family") or _engine_family(cluster.get("engine", ""))
        if isinstance(fam, str) and not CAPABILITIES.get(fam, {}).get("sql", True):
            return {
                "status": "unsupported_engine",
                "engine_family": fam,
                "cluster_id": cluster_id,
                "message": (
                    f"{fam} 리소스는 현재 단계(Phase 1)에서 챗 진단을 지원하지 않습니다."
                ),
            }
        # sql_via: Aurora reaches SQL via the RDS Data API; rds_instance (RDS for
        # MySQL / SQL Server) has sql=True but sql_via="direct" — a direct-TCP
        # path. MySQL runs here (R-3); SQL Server direct ships in R-4. This runs
        # AFTER all classification/approval logic above, so approval semantics
        # are identical to the Aurora path.
        if isinstance(fam, str) and CAPABILITIES.get(fam, {}).get("sql_via", "data_api") != "data_api":
            if "mysql" not in (cluster.get("engine") or ""):
                return {
                    "status": "unsupported_engine",
                    "engine_family": fam,
                    "cluster_id": cluster_id,
                    "message": (
                        "SQL Server 직접 실행은 이후 릴리스(R-4)에서 제공됩니다."
                    ),
                }
            # Direct-TCP MySQL. Read (is_safe) statements use db_secret_arn;
            # approved writes use the separate db_write_secret_arn (mirrors the
            # DocDB read/write secret split). Missing the needed secret → fail
            # closed with a static message (no str(e) leak).
            secret_arn = cluster.get("db_secret_arn") if is_safe else cluster.get("db_write_secret_arn")
            if not secret_arn:
                note = (
                    "read credentials not configured — set db_secret_arn"
                    if is_safe
                    else "write credentials not configured — set db_write_secret_arn"
                )
                return {
                    "status": "unsupported_engine",
                    "cluster_id": cluster_id,
                    "reason": f"{note} via PATCH /api/clusters/{{id}}/meta",
                }
            try:
                raw = client_for_cluster(cluster_id, "secretsmanager").get_secret_value(
                    SecretId=secret_arn
                ).get("SecretString") or "{}"
                creds = json.loads(raw)
            except Exception as e:
                print(f"[execute_sql] secret fetch failed for {cluster_id}: {e}")
                return {
                    "status": "execution_failed",
                    "cluster_id": cluster_id,
                    "reason": "대상 인스턴스 자격증명 조회에 실패했습니다.",
                }
            conn = None
            try:
                conn = mysql_direct.connect(
                    host=cluster.get("endpoint"),
                    port=cluster.get("port"),
                    database=cluster.get("db_name") or "mysql",
                    user=creds.get("username"),
                    password=creds.get("password"),
                )
                resp = mysql_direct.MySQLDataApiAdapter(conn).execute_statement(
                    sql=f"/* source=dbops-agent */ {sql}"
                )
            except Exception as e:
                print(f"[execute_sql] direct MySQL execution failed for {cluster_id}: {e}")
                return {
                    "status": "execution_failed",
                    "cluster_id": cluster_id,
                    "reason": "직접 연결 SQL 실행에 실패했습니다.",
                }
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    if resp is None:
        target_arn = cluster.get("cluster_arn") or os.environ.get("TARGET_CLUSTER_ARN", "")
        target_secret = cluster.get("secret_arn") or os.environ.get("TARGET_SECRET_ARN", "")
        target_db = cluster.get("db_name") or os.environ.get("TARGET_DB_NAME", "")

        if not target_arn or not target_secret:
            return {
                "status": "no_target",
                "reason": f"cluster_id={cluster_id!r} not found in registry — register it via /clusters first",
                "registry_table": _CLUSTERS_TABLE_NAME,
            }

        rds_data = boto3.client("rds-data")
        try:
            resp = rds_data.execute_statement(
                resourceArn=target_arn,
                secretArn=target_secret,
                database=target_db,
                sql=f"/* source=dbops-agent */ {sql}",
                includeResultMetadata=True,
            )
        except Exception as e:
            err = str(e)
            result = {
                "status": "execution_failed",
                "error": err,
                "cluster_id": cluster_id,
                "target_arn": target_arn,
            }
            # 프로비저닝 클러스터가 가장 흔하게 밟는 케이스: Data API(HttpEndpoint)
            # 미활성. raw boto 에러만으로는 DBA가 다음 행동을 알 수 없으므로
            # 활성화 명령까지 안내한다 (Aurora PG 14.9+/15.4+/16+, MySQL 3.07+ 지원).
            # Sv2·프로비저닝은 EnableHttpEndpoint(resource-arn) API로 켠다.
            # modify-db-cluster --enable-http-endpoint는 legacy Serverless v1
            # 전용이며 그 외 클러스터에선 조용히 무시된다(실측으로 확인).
            if "HttpEndpoint" in err or "Http endpoint" in err.lower():
                result["reason"] = (
                    f"이 클러스터는 RDS Data API(HttpEndpoint)가 비활성 상태라 SQL을 실행할 수 없습니다. "
                    f"request_approval(action_type='enable_data_api')로 활성화 승인 요청을 등록하면 "
                    f"DBA 승인 즉시 서버가 활성화합니다(다운타임 없음, 전파 1~2분). "
                    f"CLI 직접 실행도 가능: aws rds enable-http-endpoint --resource-arn {target_arn} (CLI v2). "
                    f"활성화 전까지는 라이브 SQL 기반 수집(테이블 통계·커넥션·Top Queries)도 동작하지 않습니다."
                )
            return result

    cols = [c["name"] for c in resp.get("columnMetadata", [])]
    rows = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) else f"col_{i}"
            row[col] = _decode_field(f)
        rows.append(row)
    result = {
        "status": "executed",
        "cluster_id": cluster_id,
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
    }
    # Non-SELECT (write) statements return no columns; surface the affected-row
    # count reported as numberOfRecordsUpdated. SELECTs always carry columns, so
    # this never fires for a read — the Aurora read path is byte-for-byte unchanged.
    if not cols and "numberOfRecordsUpdated" in resp:
        result["rows_affected"] = resp["numberOfRecordsUpdated"]
    return result
