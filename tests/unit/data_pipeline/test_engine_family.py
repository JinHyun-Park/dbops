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
    assert ef.CAPABILITIES["documentdb"]["findings"] == set()
    # DynamoDB findings are now enabled — ddb_* check_types from dynamodb_findings collector.
    assert ef.CAPABILITIES["dynamodb"]["findings"] == {"ddb"}

def test_dynamodb_cluster_id_is_regex_safe():
    import re
    cid = ef.dynamodb_cluster_id("123456789012", "ap-northeast-2", "my_table.v2")
    assert re.match(r"^[a-zA-Z0-9-]{1,63}$", cid)
    assert cid.startswith("ddb-")
    assert cid == ef.dynamodb_cluster_id("123456789012", "ap-northeast-2", "my_table.v2")
    assert cid != ef.dynamodb_cluster_id("123456789012", "us-east-1", "my_table.v2")
