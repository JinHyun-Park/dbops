from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_COPIES = [
    _ROOT / "api" / "clusters" / "tenancy.py",
    _ROOT / "api" / "dashboard" / "tenancy.py",
]


def test_tenancy_copies_are_byte_identical():
    contents = [p.read_bytes() for p in _COPIES]
    assert all(c == contents[0] for c in contents), (
        "api/*/tenancy.py copies drifted — keep them byte-identical"
    )
