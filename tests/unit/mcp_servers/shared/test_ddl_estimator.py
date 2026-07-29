"""Unit tests for the shared DDL estimator model."""

from mcp_servers.shared.ddl_estimator import (
    _disk_needed_mb,
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


# --- MySQL online-DDL semantics ----------------------------------------------
# Every expectation below was ACCEPT/REJECT-measured against a real mysqld
# (9.3.0, local scratch datadir) by running the statement with the clause and
# recording whether InnoDB took it. InnoDB REFUSES a clause it cannot honour
# (errors 1845 / 1846) rather than silently degrading, so an accepted
# LOCK=NONE is the engine itself saying concurrent DML is permitted.
#
# To re-measure: start a scratch mysqld, then for each shape run
#   ALTER TABLE t <change>, LOCK=NONE;
# and record ACCEPT vs "ERROR 1846 ... Try LOCK=SHARED".

_MY = "aurora-mysql"


def test_mysql_secondary_index_build_is_online():
    """The defect this suite exists for: MySQL never writes CONCURRENTLY, so the
    only online signal the classifier had was a PostgreSQL keyword, and every
    InnoDB index build was reported as "writes blocked during build" with a
    maintenance-window recommendation. Measured: LOCK=NONE is ACCEPTED for a
    secondary index, including the UNIQUE form and the CREATE INDEX form."""
    for ddl in (
        "ALTER TABLE t ADD INDEX ix(a)",
        "ALTER TABLE t ADD INDEX ix(a), ALGORITHM=INPLACE, LOCK=NONE",
        "ALTER TABLE t ADD UNIQUE INDEX uq(a)",
        "ALTER TABLE t ADD KEY ix(a)",
        "CREATE INDEX ix ON t(a) LOCK=NONE",
    ):
        c = classify_ddl(ddl.upper(), _MY)
        assert c["operation"] == "create_index", ddl
        assert c["online"] is True, ddl


def test_mysql_index_kinds_that_require_a_lock_are_not_promised_away():
    """Measured REJECT: "Fulltext index creation requires a lock" and "Do not
    support online operation on table with GIS index". Treating every MySQL
    index build as online would promise these two away."""
    for ddl in (
        "ALTER TABLE t ADD FULLTEXT INDEX ft(b)",
        "ALTER TABLE t ADD SPATIAL INDEX sp(pt)",
    ):
        c = classify_ddl(ddl.upper(), _MY)
        assert c["operation"] == "create_index", ddl
        assert c["online"] is False, ddl
        assert "blocking" in c["lock"], ddl


def test_mysql_declared_clauses_override_the_default():
    assert classify_ddl("ALTER TABLE T ADD INDEX IX(A), ALGORITHM=COPY", _MY)["online"] is False
    assert classify_ddl("ALTER TABLE T ADD INDEX IX(A), LOCK=SHARED", _MY)["online"] is False
    assert classify_ddl("ALTER TABLE T ADD INDEX IX(A), LOCK=EXCLUSIVE", _MY)["online"] is False


def test_mysql_column_type_change_is_recognized_and_always_a_rebuild():
    """MySQL spells this MODIFY/CHANGE COLUMN, which matched no branch and fell
    to `other` ("exclusive (assumed, unrecognized DDL)") with 0 MB of extra disk
    reported for what is a full table copy.

    Measured: a type change ALWAYS rebuilds on InnoDB, even a varchar WIDENING;
    both ALGORITHM=INPLACE and LOCK=NONE are rejected with "Cannot change column
    type INPLACE. Try LOCK=SHARED"."""
    for ddl in (
        "ALTER TABLE t MODIFY COLUMN a BIGINT",
        "ALTER TABLE t MODIFY COLUMN b VARCHAR(100)",
        "ALTER TABLE t CHANGE COLUMN a a2 BIGINT",
    ):
        c = classify_ddl(ddl.upper(), _MY)
        assert c["operation"] == "alter_column_type", ddl
        assert c["online"] is False, ddl
    e = estimate_ddl(
        ddl_sql="ALTER TABLE t MODIFY COLUMN a BIGINT", table="t", row_count=1,
        size_mb=500.0, instance_class="db.r6g.large", engine=_MY,
    )
    assert e["disk_space_needed_mb"] == 500.0  # a rebuild holds a second copy


def test_mysql_rewrite_advice_does_not_name_a_postgresql_tool():
    """MySQL only reaches the rewrite branch now that MODIFY COLUMN is
    classified, and that branch used to recommend pg_repack unconditionally."""
    my = estimate_ddl(
        ddl_sql="ALTER TABLE t MODIFY COLUMN a BIGINT", table="t", row_count=1,
        size_mb=100.0, instance_class="db.r6g.large", engine=_MY,
    )["recommendation"]
    assert "gh-ost" in my and "pg_repack" not in my
    pg = estimate_ddl(
        ddl_sql="ALTER TABLE t ALTER COLUMN a TYPE bigint", table="t", row_count=1,
        size_mb=100.0, instance_class="db.r6g.large", engine="aurora-postgresql",
    )["recommendation"]
    assert "pg_repack" in pg and "gh-ost" not in pg


def test_mysql_add_column_is_instant_except_stored_generated():
    """Measured ACCEPT for ALGORITHM=INSTANT with a DEFAULT (including NOT NULL
    and CURRENT_TIMESTAMP) and with GENERATED ... VIRTUAL; measured REJECT (1845)
    for GENERATED ... STORED, which materializes a value per row. The blanket
    conservative branch told the DBA to take a window for a catalog change."""
    for ddl in (
        "ALTER TABLE t ADD COLUMN e INT DEFAULT 5",
        "ALTER TABLE t ADD COLUMN e INT NOT NULL DEFAULT 7",
        "ALTER TABLE t ADD COLUMN ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE t ADD COLUMN g INT GENERATED ALWAYS AS (a+1) VIRTUAL",
    ):
        c = classify_ddl(ddl.upper(), _MY)
        assert c["online"] is True, ddl
        assert c["scans_table"] is False, ddl
    stored = classify_ddl(
        "ALTER TABLE T ADD COLUMN G INT GENERATED ALWAYS AS (A+1) STORED", _MY
    )
    assert stored["online"] is False
    assert stored["scans_table"] is True


def test_postgresql_unique_concurrently_is_no_longer_unrecognized():
    """Pre-existing and on the PRIMARY engine: classify_ddl tested the substrings
    "CREATE INDEX" / "ADD INDEX", and "CREATE UNIQUE INDEX" contains neither, so
    a CONCURRENTLY unique build was reported as an assumed exclusive lock."""
    c = classify_ddl("CREATE UNIQUE INDEX CONCURRENTLY IX ON T(A)", "aurora-postgresql")
    assert c["operation"] == "create_index"
    assert c["online"] is True
    assert resolve_table("CREATE UNIQUE INDEX CONCURRENTLY ix ON orders(a)") == "orders"
    assert resolve_table("CREATE FULLTEXT INDEX ft ON orders(b)") == "orders"


def test_postgresql_classification_is_unmoved_by_the_engine_argument():
    """The engine argument must not touch PostgreSQL, and omitting it must behave
    exactly as before it existed."""
    for engine in (None, "aurora-postgresql"):
        assert classify_ddl("CREATE INDEX I ON ORDERS (X)", engine)["online"] is False
        assert classify_ddl("CREATE INDEX CONCURRENTLY I ON ORDERS (X)", engine)["online"] is True
        assert classify_ddl("ALTER TABLE T ALTER COLUMN A TYPE BIGINT", engine)["online"] is False
        assert classify_ddl("ALTER TABLE T ADD COLUMN E INT DEFAULT 5", engine)["online"] is False
        assert classify_ddl("ALTER TABLE T ADD COLUMN E INT", engine)["online"] is True
    # Same statement, MySQL: the index build is online and ADD COLUMN DEFAULT is
    # instant. This asserts the two engines actually diverge, so a regression that
    # drops the engine plumbing cannot pass both halves of this test.
    assert classify_ddl("CREATE INDEX I ON ORDERS (X)", _MY)["online"] is True
    assert classify_ddl("ALTER TABLE T ADD COLUMN E INT DEFAULT 5", _MY)["online"] is True


def test_add_primary_key_is_online_and_expensive_at_the_same_time():
    """The one shape neither existing class expressed.

    Measured on a real mysqld: `ADD PRIMARY KEY(id), LOCK=NONE` is ACCEPTED, so
    InnoDB permits concurrent DML, while still rebuilding the table (the clustered
    index IS the table). It used to fall to the `other` fallback, which reported
    scans_table=True but 0 MB of extra disk for a full rewrite, and whose
    recommendation told the DBA to take a maintenance window they do not need.
    """
    e = estimate_ddl(
        ddl_sql="ALTER TABLE t ADD PRIMARY KEY (id)", table="t", row_count=1,
        size_mb=500.0, instance_class="db.r6g.large", engine=_MY,
    )
    assert e["operation"] == "add_primary_key"
    assert e["online_ddl_possible"] is True
    # A rebuild holds a second copy of the table, same as VACUUM FULL.
    assert e["disk_space_needed_mb"] == 500.0
    # And the recommendation must say BOTH halves: no write block, but a rewrite.
    assert "쓰기를 막지 않지만" in e["recommendation"]
    assert "재작성" in e["recommendation"]
    assert "영향 최소" not in e["recommendation"], (
        "an online-but-rebuilding operation must not be sold as minimal impact")


def test_add_primary_key_honours_a_declared_blocking_clause():
    for ddl in ("ALTER TABLE t ADD PRIMARY KEY (id), LOCK=SHARED",
                "ALTER TABLE t ADD PRIMARY KEY (id), LOCK=EXCLUSIVE",
                "ALTER TABLE t ADD PRIMARY KEY (id), ALGORITHM=COPY"):
        c = classify_ddl(ddl.upper(), _MY)
        assert c["operation"] == "add_primary_key", ddl
        assert c["online"] is False, ddl


def test_postgresql_add_primary_key_stays_blocking():
    """No new claim: PostgreSQL has no CONCURRENTLY form of ADD PRIMARY KEY, so
    the bare statement follows the same `online = has_concurrently` rule every
    other PG branch uses, and lands on False."""
    c = classify_ddl("ALTER TABLE T ADD PRIMARY KEY (ID)", "aurora-postgresql")
    assert c["operation"] == "add_primary_key"
    assert c["online"] is False
    assert "exclusive" in c["lock"]
    e = estimate_ddl(ddl_sql="ALTER TABLE t ADD PRIMARY KEY (id)", table="t",
                     row_count=1, size_mb=500.0, engine="aurora-postgresql")
    assert e["disk_space_needed_mb"] == 500.0
    assert "pg_repack" in e["recommendation"]


def test_add_primary_key_and_the_index_branch_do_not_swallow_each_other():
    """Two adjacent branches over overlapping-looking text, pinned both ways.

    What actually keeps them apart is the BRANCH ORDER: the ADD PRIMARY KEY test
    runs before the index branch. Measured: widening `_INDEX_BUILD_RX` to accept
    `ADD PRIMARY KEY` changes nothing on its own (all tests stay green), because
    the PK branch has already returned. Removing the PK branch DOES fail this
    test. So the regex exclusion is belt-and-braces, not the guarantee, and
    anyone reordering these two branches will fail here.
    """
    assert classify_ddl("ALTER TABLE T ADD PRIMARY KEY (ID)", _MY)["operation"] == "add_primary_key"
    for ddl in ("ALTER TABLE T ADD INDEX IX(A)",
                "ALTER TABLE T ADD UNIQUE INDEX UQ(A)",
                "ALTER TABLE T ADD KEY IX(A)",
                "CREATE INDEX IX ON T(A)"):
        assert classify_ddl(ddl, _MY)["operation"] == "create_index", ddl


def test_the_rebuild_set_is_one_list_not_two_literals():
    """The disk accounting and the rewrite recommendation must name the SAME set.
    They were two separate literals, which is how add_primary_key would have been
    added to one and not the other."""
    from mcp_servers.shared.ddl_estimator import _REBUILD_OPERATIONS
    for op in _REBUILD_OPERATIONS:
        assert _disk_needed_mb(op, 100.0) == 100.0, op
    assert _disk_needed_mb("create_index", 100.0) == 50.0
    assert _disk_needed_mb("drop_column", 100.0) == 0.0
