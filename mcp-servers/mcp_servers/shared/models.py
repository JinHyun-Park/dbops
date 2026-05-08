from dataclasses import dataclass
from typing import Optional


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    row_count: int


@dataclass
class MetricPoint:
    timestamp: str
    value: float
    dimensions: Optional[dict] = None
