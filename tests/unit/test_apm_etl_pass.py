import importlib.util
from pathlib import Path

_H = Path(__file__).resolve().parents[2] / "data-pipeline/etl_collector/handler.py"


def test_handler_imports_collect_apm_and_defines_pass():
    src = _H.read_text()
    assert "from collectors.apm_collector import collect_apm" in src
    assert "_collect_apm_targets" in src
    assert "APM_TARGETS_TABLE" in src
