import os
from typing import Optional

import boto3

from mcp_servers.shared.models import QueryResult


def is_mysql_engine(engine) -> bool:
    """True for ANY MySQL engine string: `aurora-mysql` AND standalone `mysql`.

    Matching both is deliberate, not sloppy. The InnoDB facts these callers
    branch on (no dead tuples, no VACUUM, DATA_FREE instead) are identical for
    Aurora MySQL and RDS MySQL, and both collectors populate table_stats from
    the SAME information_schema.tables.DATA_FREE expression
    (etl_collector/collectors/mysql_table_stats.py and
    rds_direct_collector/mysql_table_stats.py). `sqlserver` (also rds_instance)
    does NOT match.

    Where a caller needs Aurora MySQL specifically (Data API reachability), the
    handler's relational capability gate has already excluded rds_instance
    before the tool runs. See performance/handler.py _ENGINE_GATED_TOOLS.
    """
    return "mysql" in str(engine or "").lower()


class CacheClient:
    def __init__(self):
        self.rds_data = boto3.client("rds-data")
        self.cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
        self.secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
        self.database = os.environ.get("CACHE_DB_NAME", "dbops")
        self._clusters_table = os.environ.get("CLUSTERS_TABLE", "")
        self._dynamodb = None
        self._target_cache = {}
        self._engine_cache = {}

    def engine_of(self, cluster_id: str) -> str:
        """Raw `cluster_meta.engine` string for a cluster ("" when unknown).

        WHY THIS EXISTS: CAPABILITIES in engine_family.py is keyed by FAMILY, and
        Aurora PG and Aurora MySQL are the SAME family (relational). So a family
        flag cannot express a PG-vs-MySQL difference: the distinction has to be
        resolved from the engine string at the tool. Same contract as
        operations/tools/prewarm_reader._is_postgres and simulation upgrade_plan,
        hoisted here so the three performance tools that need it share one
        memoized lookup instead of three copies.

        Memoized per instance (an engine never changes), mirroring _target_cache,
        so a warm Lambda pays one cache query per cluster. Returns "" on any
        failure: callers must treat "" as "not MySQL", i.e. keep the default
        (PostgreSQL) behaviour rather than guess.
        """
        if not cluster_id:
            return ""
        if cluster_id in self._engine_cache:
            return self._engine_cache[cluster_id]
        try:
            result = self.execute(
                "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
                {"cid": cluster_id},
            )
            rows = getattr(result, "rows", None) or []
            first = rows[0] if rows else None
            engine = str(first.get("engine") or "") if isinstance(first, dict) else ""
        except Exception as e:
            print(f"[CacheClient] engine lookup failed for {cluster_id}: {e}")
            return ""
        self._engine_cache[cluster_id] = engine
        return engine

    def _resolve_target(self, cluster_id: str) -> Optional[dict]:
        """Return {cluster_arn, secret_arn, db_name} for a registered target cluster."""
        if cluster_id in self._target_cache:
            return self._target_cache[cluster_id]
        if not self._clusters_table:
            return None
        if self._dynamodb is None:
            self._dynamodb = boto3.resource("dynamodb")
        try:
            tbl = self._dynamodb.Table(self._clusters_table)
            resp = tbl.get_item(Key={"cluster_id": cluster_id})
            item = resp.get("Item")
            if not item:
                return None
            target = {
                "cluster_arn": item.get("cluster_arn", ""),
                "secret_arn": item.get("secret_arn", ""),
                "db_name": item.get("db_name", ""),
            }
            self._target_cache[cluster_id] = target
            return target
        except Exception as e:
            print(f"[CacheClient] target lookup failed for {cluster_id}: {e}")
            return None

    def execute_on_target(self, cluster_id: str, sql: str, params: Optional[dict] = None) -> QueryResult:
        """Execute SQL against the target Aurora cluster (NOT the cache DB).
        Looks up cluster_arn/secret/db from the DynamoDB clusters registry."""
        target = self._resolve_target(cluster_id)
        if not target or not target.get("cluster_arn") or not target.get("secret_arn"):
            return QueryResult(columns=[], rows=[], row_count=0)

        sql_params = []
        if params:
            for key, value in params.items():
                if isinstance(value, bool):
                    sql_params.append({"name": key, "value": {"booleanValue": value}})
                elif isinstance(value, int):
                    sql_params.append({"name": key, "value": {"longValue": value}})
                elif isinstance(value, float):
                    sql_params.append({"name": key, "value": {"doubleValue": value}})
                else:
                    sql_params.append({"name": key, "value": {"stringValue": str(value)}})

        response = self.rds_data.execute_statement(
            resourceArn=target["cluster_arn"],
            secretArn=target["secret_arn"],
            database=target.get("db_name") or "postgres",
            sql=f"/* source=dbops-agent */ {sql}",
            parameters=sql_params,
            includeResultMetadata=True,
        )

        columns = [col["name"] for col in response.get("columnMetadata", [])]
        rows = []
        for record in response.get("records", []):
            row = {}
            for i, field in enumerate(record):
                col_name = columns[i] if i < len(columns) else f"col_{i}"
                if "stringValue" in field:
                    row[col_name] = field["stringValue"]
                elif "longValue" in field:
                    row[col_name] = field["longValue"]
                elif "doubleValue" in field:
                    row[col_name] = field["doubleValue"]
                elif "booleanValue" in field:
                    row[col_name] = field["booleanValue"]
                elif "isNull" in field:
                    row[col_name] = None
                else:
                    row[col_name] = str(field)
            rows.append(row)
        return QueryResult(columns=columns, rows=rows, row_count=len(rows))

    def _build_query(
        self,
        table: str,
        cluster_id: str,
        time_column: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
        extra_where: Optional[str] = None,
    ) -> tuple[str, dict]:
        conditions = ["cluster_id = :cluster_id"]
        params = {"cluster_id": cluster_id}

        if time_column and start_time:
            conditions.append(f"{time_column} >= :start_time")
            params["start_time"] = start_time
        if time_column and end_time:
            conditions.append(f"{time_column} < :end_time")
            params["end_time"] = end_time
        if extra_where:
            conditions.append(extra_where)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {limit}"

        return sql, params

    def execute(self, sql: str, params: Optional[dict] = None) -> QueryResult:
        sql_params = []
        if params:
            for key, value in params.items():
                if isinstance(value, int):
                    sql_params.append({"name": key, "value": {"longValue": value}})
                elif isinstance(value, float):
                    sql_params.append({"name": key, "value": {"doubleValue": value}})
                else:
                    sql_params.append({"name": key, "value": {"stringValue": str(value)}})

        response = self.rds_data.execute_statement(
            resourceArn=self.cluster_arn,
            secretArn=self.secret_arn,
            database=self.database,
            sql=f"/* source=dbops-agent */ {sql}",
            parameters=sql_params,
            includeResultMetadata=True,
        )

        columns = [col["name"] for col in response.get("columnMetadata", [])]
        rows = []
        for record in response.get("records", []):
            row = {}
            for i, field in enumerate(record):
                col_name = columns[i] if i < len(columns) else f"col_{i}"
                if "stringValue" in field:
                    row[col_name] = field["stringValue"]
                elif "longValue" in field:
                    row[col_name] = field["longValue"]
                elif "doubleValue" in field:
                    row[col_name] = field["doubleValue"]
                elif "booleanValue" in field:
                    row[col_name] = field["booleanValue"]
                elif "isNull" in field:
                    row[col_name] = None
                else:
                    row[col_name] = str(field)
            rows.append(row)

        return QueryResult(columns=columns, rows=rows, row_count=len(rows))


class CrossAccountClient:
    def __init__(self, spoke_role_arn: str, region: str):
        sts = boto3.client("sts")
        credentials = sts.assume_role(
            RoleArn=spoke_role_arn,
            RoleSessionName="dbops-cross-account",
            DurationSeconds=3600,
        )["Credentials"]

        session = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=region,
        )
        self.rds = session.client("rds")
        self.pi = session.client("pi")
        self.logs = session.client("logs")
        self.rds_data = session.client("rds-data")
        self.cloudwatch = session.client("cloudwatch")
