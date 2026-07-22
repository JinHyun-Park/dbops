import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"

def _load():
    spec = importlib.util.spec_from_file_location(
        "engine_family", _ROOT / "collectors/engine_family.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

ef = _load()

def test_family_derivation():
    assert ef.engine_family("aurora-postgresql") == "relational"
    assert ef.engine_family("aurora-mysql") == "relational"
    assert ef.engine_family("docdb") == "documentdb"
    assert ef.engine_family("dynamodb") == "dynamodb"
    assert ef.engine_family("") == "relational"        # legacy default
    assert ef.engine_family(None) == "relational"

def test_capabilities_shape():
    assert ef.CAPABILITIES["relational"]["sql"] is True
    assert ef.CAPABILITIES["documentdb"]["sql"] is False
    assert ef.CAPABILITIES["dynamodb"]["rds_meta"] is False
    assert ef.CAPABILITIES["documentdb"]["cw_namespace"] == "AWS/DocDB"
    assert ef.CAPABILITIES["dynamodb"]["cw_namespace"] == "AWS/DynamoDB"
    assert ef.CAPABILITIES["relational"]["findings"] == {"health", "cost", "param_fitness", "capacity_forecast"}
    assert ef.CAPABILITIES["documentdb"]["findings"] == {"docdb"}
    # DynamoDB findings are now enabled — ddb_* check_types from dynamodb_findings collector.
    assert ef.CAPABILITIES["dynamodb"]["findings"] == {"ddb"}

def test_dynamodb_cluster_id_is_regex_safe():
    import re
    cid = ef.dynamodb_cluster_id("123456789012", "ap-northeast-2", "my_table.v2")
    assert re.match(r"^[a-zA-Z0-9-]{1,63}$", cid)
    assert cid.startswith("ddb-")
    assert cid == ef.dynamodb_cluster_id("123456789012", "ap-northeast-2", "my_table.v2")
    assert cid != ef.dynamodb_cluster_id("123456789012", "us-east-1", "my_table.v2")

def test_rds_instance_family_derivation():
    assert ef.engine_family("mysql") == "rds_instance"
    assert ef.engine_family("sqlserver-ex") == "rds_instance"
    assert ef.engine_family("sqlserver-ee") == "rds_instance"
    assert ef.engine_family("sqlserver-se") == "rds_instance"
    assert ef.engine_family("sqlserver-web") == "rds_instance"
    assert ef.engine_family("SQLServer-EX") == "rds_instance"
    # Aurora stays relational — the 'aurora' guard must win over the bare
    # 'mysql' substring.
    assert ef.engine_family("aurora-mysql") == "relational"
    assert ef.engine_family("aurora-postgresql") == "relational"

def test_rds_instance_capabilities():
    caps = ef.CAPABILITIES["rds_instance"]
    assert caps["sql"] is True
    assert caps["sql_via"] == "direct"
    assert ef.CAPABILITIES["relational"]["sql_via"] == "data_api"
    assert caps["rds_meta"] is True
    assert caps["perf_insights"] is True
    assert caps["simulation"] is False
    assert caps["custom_endpoint"] is False
    assert caps["prewarm"] is False
    assert caps["scale_instance"] is False
    assert caps["cw_namespace"] == "AWS/RDS"
    # R-2: cache-only MySQL param_fitness now runs in the ETL collector.
    assert caps["findings"] == {"param_fitness"}

def test_all_python_copies_are_verbatim_identical():
    root = Path(__file__).resolve().parents[3]
    paths = [
        root / "api" / "clusters" / "engine_family.py",
        root / "api" / "dashboard" / "engine_family.py",
        root / "data-pipeline" / "etl_collector" / "collectors" / "engine_family.py",
        root / "mcp-servers" / "mcp_servers" / "shared" / "engine_family.py",
    ]
    contents = [p.read_text() for p in paths]
    assert all(c == contents[0] for c in contents[1:]), "engine_family.py copies diverged"
