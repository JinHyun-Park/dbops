import os
import boto3
from typing import Optional
from mcp_servers.shared.models import QueryResult


class CacheClient:
    def __init__(self):
        self.rds_data = boto3.client("rds-data")
        self.cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
        self.secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
        self.database = os.environ.get("CACHE_DB_NAME", "dbops")

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
