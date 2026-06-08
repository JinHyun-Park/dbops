"""Unit tests for the shared DDL estimator model."""

from mcp_servers.shared.ddl_estimator import (
    classify_ddl,
    estimate_ddl,
    resolve_table,
    throughput_mb_s,
)

# --- table resolution ---------------------------------------------------------


def test_resolve_table_variants():
    assert resolve_table("ALTER TABLE orders ADD COLUMN x int") == "orders"
    assert resolve_table("CREATE INDEX CONCURRENTLY idx ON public.orders (x)") == "orders"
    assert resolve_table('DROP TABLE "Sales"') == "Sales"
    # Quoted qualified name must resolve to the bare table, not '"orders'.
    assert resolve_table('ALTER TABLE "public"."orders" ADD COLUMN x int') == "orders"
    assert resolve_table("SELECT 1") is None


# --- throughput grounding (the B1 fix) ----------------------------------------


def test_throughput_scales_with_instance_size():
    medium, _, _ = throughput_mb_s("db.r6g.medium", False)
    large, _, _ = throughput_mb_s("db.r6g.large", False)
    x4, _, _ = throughput_mb_s("db.r6g.4xlarge", False)
    assert medium < large < x4


def test_io_optimized_increases_throughput():
    std, _, _ = throughput_mb_s("db.r6g.4xlarge", False)
    io, _, _ = throughput_mb_s("db.r6g.4xlarge", True)
    assert io > std


def test_unknown_instance_falls_back_low_confidence_flag():
    mb_s, factors, known = throughput_mb_s(None, False)
    assert mb_s > 0
    assert known is False


# --- the core estimate --------------------------------------------------------


def _index_ddl():
    return "CREATE INDEX idx_orders_date ON orders (order_date)"


def test_bigger_instance_makes_scan_ddl_faster():
    small = estimate_ddl(
        ddl_sql=_index_ddl(), table="orders", row_count=10_000_000, size_mb=4000,
        instance_class="db.r6g.large", io_optimized=False,
    )
    big = estimate_ddl(
        ddl_sql=_index_ddl(), table="orders", row_count=10_000_000, size_mb=4000,
        instance_class="db.r6g.16xlarge", io_optimized=False,
    )
    assert big["estimated_seconds"] < small["estimated_seconds"]


def test_scan_time_scales_with_size():
    small = estimate_ddl(
        ddl_sql=_index_ddl(), table="t", row_count=1, size_mb=100,
        instance_class="db.r6g.large", io_optimized=False,
    )
    big = estimate_ddl(
        ddl_sql=_index_ddl(), table="t", row_count=1, size_mb=8000,
        instance_class="db.r6g.large", io_optimized=False,
    )
    assert big["estimated_seconds"] > small["estimated_seconds"]


def test_concurrently_is_online_and_slower():
    plain = estimate_ddl(
        ddl_sql="CREATE INDEX i ON orders (x)", table="orders", row_count=1, size_mb=2000,
        instance_class="db.r6g.large", io_optimized=False,
    )
    conc = estimate_ddl(
        ddl_sql="CREATE INDEX CONCURRENTLY i ON orders (x)", table="orders", row_count=1, size_mb=2000,
        instance_class="db.r6g.large", io_optimized=False,
    )
    assert plain["online_ddl_possible"] is False
    assert conc["online_ddl_possible"] is True
    assert conc["estimated_seconds"] > plain["estimated_seconds"]


def test_metadata_only_is_size_independent_and_high_confidence():
    huge = estimate_ddl(
        ddl_sql="ALTER TABLE users DROP COLUMN email", table="users", row_count=10**9, size_mb=8000,
        instance_class="db.r6g.large", io_optimized=False,
    )
    assert huge["operation"] == "drop_column"
    assert huge["estimated_seconds"] <= 5
    assert huge["confidence"] == "high"


def test_range_and_basis_present():
    e = estimate_ddl(
        ddl_sql=_index_ddl(), table="orders", row_count=1, size_mb=2000,
        instance_class="db.r6g.large", io_optimized=False,
    )
    lo, hi = e["estimated_range_seconds"]
    assert lo <= e["estimated_seconds"] <= hi
    assert isinstance(e["basis"], list) and e["basis"]
    assert e["throughput_mb_s"] > 0


def test_unknown_size_drops_confidence():
    e = estimate_ddl(
        ddl_sql=_index_ddl(), table="orders", row_count=0, size_mb=0,
        instance_class="db.r6g.large", io_optimized=False,
    )
    assert e["confidence"] == "low"


def test_add_column_with_default_not_online():
    e = estimate_ddl(
        ddl_sql="ALTER TABLE orders ADD COLUMN c timestamptz DEFAULT now()",
        table="orders", row_count=1, size_mb=100,
        instance_class="db.r6g.large", io_optimized=False,
    )
    assert e["online_ddl_possible"] is False
    assert "rewrite" in e["lock_type"].lower()


def test_vacuum_full_is_rewrite_with_disk():
    e = estimate_ddl(
        ddl_sql="VACUUM FULL orders", table="orders", row_count=1, size_mb=1000,
        instance_class="db.r6g.large", io_optimized=False,
    )
    assert e["operation"] == "rewrite"
    assert e["disk_space_needed_mb"] > 0
    assert "pg_repack" in e["recommendation"]


def test_classify_plain_index_blocking():
    c = classify_ddl("CREATE INDEX I ON ORDERS (X)")
    assert c["online"] is False
    assert "blocking" in c["lock"]
