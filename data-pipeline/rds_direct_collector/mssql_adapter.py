"""Data-API-shape adapter over a live pytds (SQL Server) connection.

Positional twin of mysql_adapter.MySQLDataApiAdapter: the DMV collectors read
rows BY POSITION via their _str/_long/_double helpers, so this returns only
{"records": [[field...]]} — no columnMetadata (unlike shared/mssql_direct's
adapter, which execute_sql's name-based chat path needs). The collectors live
in this Lambda's asset dir and can't import from mcp-servers, so the adapter is
vendored here; _field is byte-identical to mysql_adapter._field."""
from datetime import date, datetime
from decimal import Decimal


def _field(v):
    if v is None:
        return {"isNull": True}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"longValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, Decimal):
        return {"doubleValue": float(v)}
    if isinstance(v, bytes):
        return {"stringValue": v.decode("utf-8", errors="replace")}
    if isinstance(v, (datetime, date)):
        return {"stringValue": str(v)}
    return {"stringValue": str(v)}


class MSSQLDataApiAdapter:
    """Duck-types the subset of the rds-data client the collectors use."""

    def __init__(self, conn):
        self._conn = conn

    def execute_statement(self, resourceArn=None, secretArn=None,
                          database=None, sql=None, parameters=None, **kwargs):
        # The vendored collectors pass static SQL only (no parameters) for
        # target reads; assert loudly if that assumption ever breaks.
        if parameters:
            raise ValueError("MSSQLDataApiAdapter does not support parameters")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return {"records": [[_field(v) for v in row] for row in rows]}
