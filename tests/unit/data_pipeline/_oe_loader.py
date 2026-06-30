"""Shared loader for outcome_evaluator unit tests.

outcome_evaluator uses bare imports (import case_opener, import evaluator, etc.)
because the Lambda asset bundles the directory's CONTENTS to /var/task. Tests
insert the package dir onto sys.path so the same bare imports work locally.

Each test module calls load() at import time and teardown_module() at the end
to remove what THIS module added — keeping the suite order-independent.
"""
import importlib.util
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[3] / "data-pipeline" / "outcome_evaluator"


def load(mod_name: str, file_name: str | None = None):
    """Load a module from PKG by file name; register in sys.modules under mod_name."""
    spec = importlib.util.spec_from_file_location(
        mod_name, PKG / f"{file_name or mod_name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def install_path() -> bool:
    """Insert PKG onto sys.path if not already present. Returns True if inserted."""
    key = str(PKG)
    if key not in sys.path:
        sys.path.insert(0, key)
        return True
    return False


def teardown(added_path: bool, *mod_names: str) -> None:
    """Remove PKG from sys.path (only if we added it) and pop mod_names from sys.modules."""
    if added_path:
        key = str(PKG)
        try:
            sys.path.remove(key)
        except ValueError:
            pass
    for name in mod_names:
        sys.modules.pop(name, None)
