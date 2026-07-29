"""Unit tests for the shared upgrade_estimator model.

These pin the methodology the model encodes (researched from AWS docs):
  - MAJOR upgrade time is driven by OBJECT COUNT, not raw storage.
  - MINOR upgrade time is ~size-independent (binary swap + reboot).
  - blue/green downtime is sub-minute (switchover) regardless of size.
  - in-place MAJOR downtime grows with object count (pg_upgrade window).
  - estimates carry a range, a confidence, and a methodology note.
"""

from mcp_servers.shared.upgrade_estimator import (
    classify_upgrade,
    estimate_upgrade,
    major_family,
    major_jump,
)


def _method(est, name):
    return next(m for m in est["methods"] if m["method"] == name)


# --- classification / parsing -------------------------------------------------


def test_classify_pg_major_vs_minor():
    assert classify_upgrade("15.4", "16.2") == "major"
    assert classify_upgrade("15.4", "15.7") == "minor"


def test_classify_mysql_aurora_family():
    assert classify_upgrade("8.0.mysql_aurora.2.11.0", "8.0.mysql_aurora.3.06.0") == "major"
    assert classify_upgrade("8.0.mysql_aurora.3.04.0", "8.0.mysql_aurora.3.06.0") == "minor"


def test_unparseable_is_major():
    assert classify_upgrade("unknown", "15.5") == "major"


def test_major_jump_distance():
    assert major_jump("12.4", "16.2") == 4
    assert major_jump("15.4", "15.7") == 0
    assert major_jump("unknown", "16.2") == 1  # major but distance unknown -> 1


def test_major_family_tokens():
    assert major_family("15.4") == "15"
    assert major_family("8.0.mysql_aurora.3.06.0") == "mysql_aurora.3"
    assert major_family("") == ""


def test_standalone_rds_mysql_major_boundary_is_the_second_component():
    """MySQL's major version is the FIRST TWO components, not the leading int.

    Measured against AWS: describe-db-engine-versions --engine mysql
    --engine-version 8.0.42 lists 8.4.6 with ``IsMajorVersionUpgrade: true``,
    while the leading integer is 8 on both sides. Before the engine argument
    existed this returned "minor" with major_jump 0 and confidence "high", and
    the REST /upgrade-impact route has NO engine-family gate, so a registered
    standalone RDS MySQL cluster reached it (reproduced live on dbops-demo-mysql).
    """
    assert classify_upgrade("8.0.42", "8.4.6", "mysql") == "major"
    assert classify_upgrade("8.0.35", "8.0.42", "mysql") == "minor"
    assert major_family("8.4.9", "mysql") == "8.4"
    # The `-rds.YYYYMMDD` build-suffix form RDS actually reports.
    assert major_family("5.7.44-rds.20250213", "mysql") == "5.7"


def test_mysql_jump_is_a_ladder_distance_not_arithmetic():
    """_family_int would read "8.0" as 0 and "8.4" as 4: FOUR majors for one
    family step. The ladder gives the number of release families crossed."""
    assert major_jump("8.0.42", "8.4.6", "mysql") == 1
    assert major_jump("5.7.44", "8.0.42", "mysql") == 1  # was 3 (8 - 5)
    assert major_jump("5.7.44", "8.4.6", "mysql") == 2   # two families crossed
    # A downgrade floors at 0, matching the pre-existing PG convention.
    assert major_jump("8.4.9", "8.0.42", "mysql") == 0
    # A family off the ladder (a future MySQL release) falls back to the safe
    # "1 if major" rather than inventing a distance.
    assert major_jump("9.1.0", "8.4.6", "mysql") == 1


def test_aurora_and_pg_are_unmoved_by_the_engine_argument():
    """The engine argument must not touch the two families that were correct.

    Aurora MySQL carries its family in the version string ("mysql_aurora.3"),
    which is read BEFORE the engine is consulted, and PostgreSQL / SQL Server
    keep the leading-integer rule.
    """
    assert classify_upgrade("8.0.mysql_aurora.2.11.0", "8.0.mysql_aurora.3.06.0", "aurora-mysql") == "major"
    assert classify_upgrade("8.0.mysql_aurora.3.04.0", "8.0.mysql_aurora.3.06.0", "aurora-mysql") == "minor"
    assert major_family("8.0.mysql_aurora.3.06.0", "aurora-mysql") == "mysql_aurora.3"
    assert classify_upgrade("15.4", "16.2", "aurora-postgresql") == "major"
    assert major_jump("12.4", "16.2", "aurora-postgresql") == 4
    assert classify_upgrade("15.00.4470.1.v1", "16.00.4085.2.v1", "sqlserver-ex") == "major"


def test_engine_argument_is_optional_and_defaults_to_previous_behaviour():
    """Callers that do not know the engine must be unaffected: the argument was
    added to an already-shipped signature used by both surfaces."""
    assert major_family("15.4") == "15"
    assert major_family("8.0.mysql_aurora.3.06.0") == "mysql_aurora.3"
    assert classify_upgrade("15.4", "16.2") == "major"
    assert major_jump("12.4", "16.2") == 4
    # Without the engine, a plain MySQL pair still reads as the old rule did.
    assert classify_upgrade("8.0.42", "8.4.6") == "minor"


def test_estimate_upgrade_passes_engine_into_classification():
    """The estimator takes `engine` already; the bug was that it did not forward
    it. A standalone RDS MySQL family change must come back as a major."""
    est = estimate_upgrade(
        engine="mysql",
        current_version="8.0.42",
        target_version="8.4.6",
        storage_gb=100,
        readers=0,
        table_count=500,
    )
    assert est["upgrade_type"] == "major"
    assert est["major_jump"] == 1


def test_jump_does_not_mix_engine_kinds():
    """A MySQL family number must never be diffed against a PG integer. If a
    MySQL current ('mysql_aurora.2') is compared to a bare '8.0' target, the
    jump must fall back to 1 (major), not an absurd 8 - 2 = 6."""
    assert major_jump("8.0.mysql_aurora.2.11.0", "8.0") == 1
    # Same family => real numeric distance.
    assert major_jump("8.0.mysql_aurora.2.11.0", "8.0.mysql_aurora.3.06.0") == 1


# --- the core methodology -----------------------------------------------------


def test_major_time_driven_by_object_count_not_storage():
    """Two MAJOR upgrades, same storage: more tables => more time. And a huge
    storage / few tables DB must not beat a small storage / many tables one."""
    few = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=100, readers=0, table_count=500,
    )
    many = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=100, readers=0, table_count=50000,
    )
    assert _method(many, "in_place")["estimated_minutes"] > _method(few, "in_place")["estimated_minutes"]

    big_storage_few_tables = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=4000, readers=0, table_count=500,
    )
    small_storage_many_tables = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=50, readers=0, table_count=50000,
    )
    # Object count must dominate over raw storage for a major.
    assert (
        _method(small_storage_many_tables, "in_place")["estimated_minutes"]
        > _method(big_storage_few_tables, "in_place")["estimated_minutes"]
    )


def test_minor_is_size_independent_relative_to_major():
    """A MINOR upgrade on a huge DB is far cheaper than a MAJOR on a small one."""
    minor_huge = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="15.7",
        storage_gb=4000, readers=0, table_count=50000,
    )
    major_small = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=50, readers=0, table_count=2000,
    )
    assert _method(minor_huge, "in_place")["estimated_minutes"] < _method(major_small, "in_place")["estimated_minutes"]


def test_blue_green_downtime_is_subminute_regardless_of_size():
    est = estimate_upgrade(
        engine="aurora-postgresql", current_version="12.4", target_version="16.2",
        storage_gb=8000, readers=4, table_count=100000,
    )
    bg = _method(est, "blue_green")
    assert bg["downtime_seconds"] < 60  # sub-minute switchover
    assert "switchover" in bg["downtime_text"].lower()


def test_in_place_major_downtime_grows_with_objects():
    small = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=100, readers=0, table_count=500,
    )
    large = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=100, readers=0, table_count=80000,
    )
    assert _method(large, "in_place")["downtime_seconds"] > _method(small, "in_place")["downtime_seconds"]


def test_confidence_levels():
    # minor -> high
    minor = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="15.7",
        storage_gb=100, readers=0, table_count=1000,
    )
    assert minor["confidence"] == "high"
    # major with object count -> medium
    major_known = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=100, readers=0, table_count=1000,
    )
    assert major_known["confidence"] == "medium"
    # major without object count -> low + flagged
    major_unknown = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=100, readers=0, table_count=None,
    )
    assert major_unknown["confidence"] == "low"
    assert "미상" in major_unknown["object_count_basis"]


def test_each_method_has_range_and_basis():
    est = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=200, readers=1, table_count=3000,
    )
    for m in est["methods"]:
        assert m["range_low_minutes"] <= m["estimated_minutes"] <= m["range_high_minutes"]
        assert isinstance(m["basis"], list) and m["basis"]
    assert est["methodology_note"]
    assert "clone" in est["methodology_note"].lower()


def test_recommendation_matches_legacy_rules():
    # major -> blue_green
    major = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="16.2",
        storage_gb=100, readers=0, table_count=1000,
    )
    assert major["recommendation"] == "blue_green"
    # minor small -> in_place
    minor_small = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="15.7",
        storage_gb=50, readers=0, table_count=100,
    )
    assert minor_small["recommendation"] == "in_place"
    # minor large -> blue_green
    minor_large = estimate_upgrade(
        engine="aurora-postgresql", current_version="15.4", target_version="15.7",
        storage_gb=800, readers=0, table_count=100,
    )
    assert minor_large["recommendation"] == "blue_green"
