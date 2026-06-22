import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "data-pipeline" / "etl_collector" / "collectors" / "meta_collector.py"
_spec = importlib.util.spec_from_file_location("meta_collector", PATH)
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)


def test_build_instance_list_roles_and_class():
    rds = MagicMock()
    rds.describe_db_instances.return_value = {
        "DBInstances": [
            {"DBInstanceIdentifier": "w1", "DBInstanceClass": "db.r6g.large"},
            {"DBInstanceIdentifier": "r1", "DBInstanceClass": "db.r6g.large"},
        ]
    }
    members = [
        {"DBInstanceIdentifier": "w1", "IsClusterWriter": True},
        {"DBInstanceIdentifier": "r1", "IsClusterWriter": False},
    ]
    out = mc._build_instance_list(rds, "c1", members)
    assert {"id": "w1", "role": "writer", "class": "db.r6g.large"} in out
    assert {"id": "r1", "role": "reader", "class": "db.r6g.large"} in out


def test_build_instance_list_empty_on_error():
    rds = MagicMock()
    rds.describe_db_instances.side_effect = RuntimeError("denied")
    members = [{"DBInstanceIdentifier": "w1", "IsClusterWriter": True}]
    # falls back to role-only entries (class "") — never raises
    out = mc._build_instance_list(rds, "c1", members)
    assert out == [{"id": "w1", "role": "writer", "class": ""}]
