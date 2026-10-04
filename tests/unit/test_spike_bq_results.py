"""Spike (iteration 2): BigQuery result handling.

Decision: query results are consumed with ``result.to_arrow()`` or by iterating rows
(``for row in result`` / ``[dict(r) for r in result]``). ``result.to_dataframe()`` is NOT used: it
needs pandas and db-dtypes, converts types implicitly (NUMERIC to object,
timestamps to pandas types),
and the agent only needs a small capped table of plain values.
Consequence: ``pandas`` and ``db-dtypes`` are not runtime dependencies of this project.
"""

from __future__ import annotations


class FakeRowIterator:
    """Stands in for ``google.cloud.bigquery.table.RowIterator`` (no network)."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.used: list[str] = []

    def __iter__(self):
        self.used.append("iter")
        return iter(self._rows)

    def to_arrow(self):
        self.used.append("to_arrow")
        import pyarrow as pa

        return pa.Table.from_pylist(self._rows)

    def to_dataframe(self, *args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("to_dataframe must not be used")


def test_rows_are_read_by_iteration_not_dataframe():
    result = FakeRowIterator([{"city": "Berlin", "n": 7}, {"city": "Paris", "n": 9}])
    rows = [dict(r) for r in result]
    assert rows == [{"city": "Berlin", "n": 7}, {"city": "Paris", "n": 9}]
    assert result.used == ["iter"]


def test_arrow_path_gives_plain_python_values():
    result = FakeRowIterator([{"city": "Berlin", "n": 7}])
    table = result.to_arrow()
    assert table.to_pylist() == [{"city": "Berlin", "n": 7}]
    assert result.used == ["to_arrow"]
