"""split_total_count — the seam that turns a capped list into an honest one.

Every list-shaped tool caps its rows, which is correct: an uncapped query
against a real instance is the active_sessions incident waiting to happen.
Without a total, though, 50 rows back from 688 failed requests reads as the
whole answer and nothing in the response says otherwise. These pin the
contract that prevents that, including the cases where there is no total to
report and inventing one would be worse than saying nothing.
"""

from __future__ import annotations

from decimal import Decimal

from ebsmcp.tools.registry import TOTAL_COUNT_COLUMN, split_total_count


def test_total_is_lifted_out_and_rows_keep_their_own_fields():
    rows = [{"sid": 1, TOTAL_COUNT_COLUMN: 688}, {"sid": 2, TOTAL_COUNT_COLUMN: 688}]
    stripped, total = split_total_count(rows)
    assert total == 688
    assert stripped == [{"sid": 1}, {"sid": 2}]


def test_the_column_is_removed_from_every_row_not_just_the_first():
    """It repeats identically on each row, and these payloads are read by a
    model with a context budget — one number belongs in the envelope, not
    duplicated fifty times."""
    rows = [{"n": i, TOTAL_COUNT_COLUMN: 9} for i in range(5)]
    stripped, _ = split_total_count(rows)
    assert all(TOTAL_COUNT_COLUMN not in row for row in stripped)
    assert [r["n"] for r in stripped] == [0, 1, 2, 3, 4]


def test_a_query_without_the_column_passes_through_untouched():
    """Uncapped queries never get a windowed count — open_cursors' sid branch
    is exactly that — and must not be mangled on the way through."""
    rows = [{"sid": 1, "sql_text": "select 1"}]
    stripped, total = split_total_count(rows)
    assert total is None
    assert stripped == rows


def test_no_rows_means_no_total_rather_than_zero():
    """Zero is a claim about the data; None is the honest "the query reported
    nothing", and the two must not be confused by a caller."""
    stripped, total = split_total_count([])
    assert stripped == []
    assert total is None


def test_total_comes_back_as_an_int():
    """Oracle returns numerics as Decimal. A Decimal in the payload serialises
    differently and reads as a type accident to whoever consumes it."""
    _, total = split_total_count([{"x": 1, TOTAL_COUNT_COLUMN: Decimal("688")}])
    assert total == 688
    assert isinstance(total, int)


def test_a_null_total_does_not_crash():
    _, total = split_total_count([{"x": 1, TOTAL_COUNT_COLUMN: None}])
    assert total is None
