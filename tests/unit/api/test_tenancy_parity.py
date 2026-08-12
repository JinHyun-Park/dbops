from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_COPIES = [
    _ROOT / "api" / "clusters" / "tenancy.py",
    _ROOT / "api" / "dashboard" / "tenancy.py",
    _ROOT / "api" / "reports" / "tenancy.py",
    _ROOT / "api" / "saved_queries" / "tenancy.py",
    _ROOT / "api" / "approvals" / "tenancy.py",
    _ROOT / "api" / "alerts" / "tenancy.py",
    _ROOT / "api" / "tasks" / "tenancy.py",
    _ROOT / "api" / "scheduled_tasks" / "tenancy.py",
    _ROOT / "api" / "cost" / "tenancy.py",
    _ROOT / "api" / "simulation" / "tenancy.py",
    _ROOT / "api" / "apm" / "tenancy.py",
]


def test_tenancy_copies_are_byte_identical():
    contents = [p.read_bytes() for p in _COPIES]
    assert all(c == contents[0] for c in contents), (
        "api/*/tenancy.py copies drifted — keep them byte-identical"
    )
