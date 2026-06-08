from unittest.mock import MagicMock

from mcp_servers.operations.tools.get_runbook import (
    _extract_sql_steps,
    get_runbook_impl,
)
from mcp_servers.shared.models import QueryResult

_RUNBOOK_BODY = """## 증상
idle-in-transaction 세션이 누적됨.

## 진단
```sql
SELECT pid, state, query_start
FROM pg_stat_activity
WHERE state = 'idle in transaction';
```

## 조치
오래된 세션을 정리한다.

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND query_start < NOW() - INTERVAL '10 min';
```

참고용 파이썬 (실행 대상 아님):
```python
print("not a step")
```
"""


def _row(rid=7, title="idle-in-tx cleanup", body=_RUNBOOK_BODY, tags=None):
    return {
        "id": rid,
        "cluster_id": "prod-pg-1",
        "title": title,
        "summary_md": "idle 세션 정리",
        "body_md": body,
        "tags": tags if tags is not None else ["idle-in-tx", "autovacuum"],
        "source": "manual",
        "source_ref": None,
        "created_by": "alice",
        "created_at": "2026-06-01T10:00:00",
    }


def test_get_runbook_by_id():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=[], rows=[_row()], row_count=1
    )
    result = get_runbook_impl(mock_cache, runbook_id="7")
    assert result["runbook"]["id"] == 7
    assert result["runbook"]["title"] == "idle-in-tx cleanup"
    assert result["runbook"]["tags"] == ["idle-in-tx", "autovacuum"]
    # Two ```sql blocks; the ```python block must NOT be a step.
    assert len(result["steps"]) == 2
    assert result["steps"][0]["n"] == 1
    assert result["steps"][1]["n"] == 2
    assert "pg_stat_activity" in result["steps"][0]["sql"]
    assert "pg_terminate_backend" in result["steps"][1]["sql"]
    assert "print(" not in str(result["steps"])
    # The guidance must steer the agent to the approval-gated path.
    assert "execute_sql" in result["note"]
    assert "approval" in result["note"].lower()
    # By-id queried with the numeric id param.
    args, kwargs = mock_cache.execute.call_args
    assert args[1] == {"id": 7}


def test_get_runbook_by_id_accepts_int_like_string():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=[], rows=[_row(rid=42)], row_count=1
    )
    result = get_runbook_impl(mock_cache, runbook_id="42")
    assert result["runbook"]["id"] == 42
    assert "error" not in result


def test_get_runbook_fuzzy_single_match():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=[], rows=[_row()], row_count=1
    )
    result = get_runbook_impl(mock_cache, query="idle")
    assert result["runbook"]["id"] == 7
    assert len(result["steps"]) == 2
    # Single match → no candidates list surfaced.
    assert "candidates" not in result
    # Fuzzy search uses an ILIKE wildcard parameter.
    args, kwargs = mock_cache.execute.call_args
    assert args[1] == {"like": "%idle%"}


def test_get_runbook_fuzzy_ambiguous_returns_candidates():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=[],
        rows=[
            _row(rid=7, title="idle-in-tx cleanup"),
            _row(rid=9, title="idle connection reaper", tags=["idle-in-tx"]),
        ],
        row_count=2,
    )
    result = get_runbook_impl(mock_cache, query="idle")
    # Best (most recent / first) match becomes the primary runbook.
    assert result["runbook"]["id"] == 7
    assert "candidates" in result
    assert {c["id"] for c in result["candidates"]} == {7, 9}


def test_get_runbook_not_found_by_id():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_runbook_impl(mock_cache, runbook_id="999")
    assert result["runbook"] is None
    assert result["steps"] == []
    assert "error" in result
    assert "999" in result["error"]


def test_get_runbook_not_found_by_query():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_runbook_impl(mock_cache, query="nonexistent")
    assert result["runbook"] is None
    assert result["steps"] == []
    assert result["candidates"] == []
    assert "error" in result


def test_get_runbook_invalid_id():
    mock_cache = MagicMock()
    result = get_runbook_impl(mock_cache, runbook_id="not-a-number")
    assert result["runbook"] is None
    assert "invalid runbook_id" in result["error"]
    # Should never have hit the DB.
    mock_cache.execute.assert_not_called()


def test_get_runbook_requires_an_argument():
    mock_cache = MagicMock()
    result = get_runbook_impl(mock_cache)
    assert result["runbook"] is None
    assert "error" in result
    mock_cache.execute.assert_not_called()


def test_get_runbook_no_sql_blocks_changes_note():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=[],
        rows=[_row(body="## 증상\n프로즈만 있고 SQL 블록은 없음.")],
        row_count=1,
    )
    result = get_runbook_impl(mock_cache, runbook_id="7")
    assert result["steps"] == []
    assert "No fenced" in result["note"]
    # Content is still returned verbatim for the agent to read.
    assert "프로즈만" in result["content"]


def test_extract_sql_steps_contiguous_numbering_skips_empty():
    body = "```sql\n\n```\n\n```sql\nSELECT 1;\n```\n\n```SQL\nSELECT 2;\n```"
    steps = _extract_sql_steps(body)
    # Empty block dropped; remaining two renumbered 1, 2. Case-insensitive tag.
    assert [s["n"] for s in steps] == [1, 2]
    assert steps[0]["sql"] == "SELECT 1;"
    assert steps[1]["sql"] == "SELECT 2;"
