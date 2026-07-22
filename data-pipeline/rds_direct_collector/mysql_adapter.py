"""Data-API-shape adapter over a live pymysql connection.

The vendored mysql_* collectors were written against RDS Data API
(execute_statement returning {"records": [[{"stringValue":...}, ...]]}).
RDS for MySQL has no Data API, so this adapter runs their SQL over a direct
pymysql connection and re-encodes rows in the exact field-dict shape the
collectors' _str/_long/_double helpers unwrap — the collectors stay verbatim
copies of the Aurora versions (parity-tested)."""
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


class MySQLDataApiAdapter:
    """Duck-types the subset of the rds-data client the collectors use."""

    def __init__(self, conn):
        self._conn = conn

    def execute_statement(self, resourceArn=None, secretArn=None,
                          database=None, sql=None, parameters=None, **kwargs):
        # The vendored collectors pass static SQL only (no parameters) for
        # target reads; assert loudly if that assumption ever breaks.
        if parameters:
            raise ValueError("MySQLDataApiAdapter does not support parameters")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return {"records": [[_field(v) for v in row] for row in rows]}
