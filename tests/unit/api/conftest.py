"""Make every api/ handler importable when its test file runs ALONE.

The REST handlers are Lambda entry points, so they bare-import their sibling
modules (`import tenancy`, `import engine_family`, ...): in Lambda the handler's
own directory is on sys.path, so that resolves. The tests load a handler by file
path with importlib, which does NOT put its directory on sys.path, so the bare
import only resolved when some EARLIER test module happened to have inserted that
directory first.

Measured on the tree before this file: 8 modules under tests/unit/api could not
be collected on their own (ModuleNotFoundError: No module named 'tenancy'), while
the full suite was green. That is the worst shape a test can have, because the
targeted run you use to check a mutation fails for a reason unrelated to the code
under test, and reads as "my change broke it".

APPEND, not insert(0): this is purely additive, so a test that deliberately
inserts its own directory at the front still wins and nothing that resolves today
starts resolving somewhere else.

Safe because there is no ambiguity to introduce: every module name duplicated
across these directories is byte-identical (tenancy.py exists 10 times,
engine_family.py 3 times), which
tests/unit/test_shared_copy_parity.py now enforces. `handler.py` is NOT exposed
by name anywhere (no test bare-imports it), which is why 30 directories carrying
30 different handler.py files can share sys.path harmlessly.
"""

import sys
from pathlib import Path

_API = Path(__file__).resolve().parents[3] / "api"

for _d in sorted(p for p in _API.iterdir() if p.is_dir()):
    _s = str(_d)
    if _s not in sys.path:
        sys.path.append(_s)
