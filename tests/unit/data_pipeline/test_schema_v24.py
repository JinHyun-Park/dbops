import pathlib

SQL = (pathlib.Path(__file__).parents[3]
       / "data-pipeline/schema_migrator/sql/schema_v24.sql").read_text()

def test_declares_three_objects():
    assert "CREATE TABLE IF NOT EXISTS remediation_cases" in SQL
    assert "CREATE TABLE IF NOT EXISTS remediation_outcomes_agg" in SQL
    # Partial unique index is what makes "one open case per symptom" enforceable.
    assert "ux_remediation_cases_open" in SQL
    assert "WHERE status = 'open'" in SQL

def test_agg_primary_key_is_three_cols():
    assert "PRIMARY KEY (cluster_id, symptom_class, action_class)" in SQL
