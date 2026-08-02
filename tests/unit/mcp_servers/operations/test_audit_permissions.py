"""audit_permissions: dialect comes from cluster_meta, never from a default.

These tests were rewritten on 2026-08-02 after a live probe found the tool broken
on Aurora MySQL. The old suite passed `engine=` on every call, so it only ever
exercised the branch it named and never the RESOLUTION, which is where the bug
was: `engine` defaulted to "postgresql", so a real Aurora MySQL cluster got
`FROM pg_roles` and answered MySQL error 1146.

So the cache mock here stubs `execute` (the cluster_meta lookup) as well as
`execute_on_target` (the target query). A MagicMock alone cannot catch the bug,
because `cache.execute(...)` then returns a MagicMock whose `.rows` is not a list
and resolution silently yields "".
"""

from unittest.mock import MagicMock

import pytest
from mcp_servers.operations.tools.audit_permissions import audit_permissions_impl
from mcp_servers.shared.models import QueryResult


def _cache(engine, target_result=None):
    """A cache whose cluster_meta says `engine`, and whose target query returns
    `target_result` (default: two PG-shaped rows, one of them a superuser)."""
    c = MagicMock()
    c.execute.return_value = QueryResult(
        columns=["engine"], rows=[{"engine": engine}], row_count=1)
    c.execute_on_target.return_value = target_result or QueryResult(
        columns=["username", "is_superuser"],
        rows=[{"username": "admin", "is_superuser": True},
              {"username": "app_user", "is_superuser": False}],
        row_count=2,
    )
    return c


def _sql_run(cache):
    return cache.execute_on_target.call_args.args[1]


# --------------------------------------------------------------------------
# dialect selection, the thing that was broken
# --------------------------------------------------------------------------

def test_aurora_postgres_uses_the_pg_catalog():
    cache = _cache("aurora-postgresql")
    result = audit_permissions_impl(cache, cluster_id="prod-pg-1")
    assert result["status"] == "ok"
    assert result["dialect"] == "postgresql"
    assert "pg_roles" in _sql_run(cache)
    assert result["total_users"] == 2
    assert result["superuser_count"] == 1
    assert "admin" in result["warnings"][0]


def test_aurora_mysql_uses_the_mysql_catalog_without_being_told():
    """The regression test for the live failure: no `engine` argument is passed,
    and the tool must still pick mysql.user rather than pg_roles."""
    cache = _cache("aurora-mysql", QueryResult(
        columns=["username", "host", "is_superuser", "can_grant"],
        rows=[{"username": "root", "host": "localhost", "is_superuser": "true", "can_grant": "true"},
              {"username": "app", "host": "%", "is_superuser": "false", "can_grant": "false"}],
        row_count=2,
    ))
    result = audit_permissions_impl(cache, cluster_id="prod-mysql-1")
    assert result["status"] == "ok"
    assert result["dialect"] == "mysql"
    sql = _sql_run(cache)
    assert "mysql.user" in sql
    assert "pg_roles" not in sql, "PG catalog on a MySQL cluster is the shipped bug"
    assert result["superuser_count"] == 1


def test_explicit_engine_overrides_only_the_dialect_not_the_family():
    """`engine="mysql"` classifies as rds_instance on its own, so deriving the
    FAMILY from the override would refuse a legitimate Aurora MySQL call. The
    family must come from cluster_meta."""
    cache = _cache("aurora-mysql", QueryResult(
        columns=["username", "is_superuser"],
        rows=[{"username": "root", "is_superuser": "true"}], row_count=1))
    result = audit_permissions_impl(cache, cluster_id="c1", engine="mysql")
    assert result["status"] == "ok"
    assert result["dialect"] == "mysql"
    assert "mysql.user" in _sql_run(cache)


def test_no_superusers_yields_no_warnings():
    cache = _cache("aurora-postgresql", QueryResult(
        columns=["username", "is_superuser"],
        rows=[{"username": "app_user", "is_superuser": False}], row_count=1))
    result = audit_permissions_impl(cache, cluster_id="prod-pg-1")
    assert result["superuser_count"] == 0
    assert result["warnings"] == []


# --------------------------------------------------------------------------
# refusals: each says something TRUE about why
# --------------------------------------------------------------------------

@pytest.mark.parametrize("engine,family", [
    ("mysql", "rds_instance"),          # standalone RDS: SQL, but no Data API
    ("sqlserver-ex", "rds_instance"),
    ("docdb", "documentdb"),            # no SQL user catalog at all
    ("valkey", "elasticache"),
])
def test_engines_without_the_data_api_are_refused_not_misreported(engine, family):
    """The old code returned "cluster not registered or unreachable — register via
    /clusters" for these, which is false: the clusters ARE registered and the real
    reason is that execute_on_target is Data-API-only."""
    cache = _cache(engine)
    result = audit_permissions_impl(cache, cluster_id="c1")
    assert result["status"] == "unsupported_engine"
    assert result["engine_family"] == family
    assert "register" not in result.get("reason", "").lower()
    cache.execute_on_target.assert_not_called()


def test_unresolvable_engine_refuses_instead_of_guessing_a_dialect():
    """engine_family() classifies "" as relational, so without this branch the
    tool would pass the Data-API gate and then pick MySQL from `"postgres" in ""`,
    querying the wrong catalog on a cluster whose engine nobody could read."""
    cache = MagicMock()
    cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = audit_permissions_impl(cache, cluster_id="mystery")
    assert result["status"] == "engine_unresolved"
    cache.execute_on_target.assert_not_called()


def test_aurora_target_unreachable_says_what_to_check():
    """An Aurora cluster with no cluster_arn/secret_arn, or Data API disabled,
    still needs an actionable message; it just must not be the same message the
    non-Data-API engines used to get."""
    cache = _cache("aurora-postgresql",
                   QueryResult(columns=[], rows=[], row_count=0))
    result = audit_permissions_impl(cache, cluster_id="prod-pg-1")
    assert result["status"] == "target_unreachable"
    assert result["total_users"] == 0
    assert result["users"] == []
    reason = result["reason"]
    assert "cluster_arn" in reason and "Data API" in reason
