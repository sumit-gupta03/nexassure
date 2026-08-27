# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Expectation reduction and comparison."""

from __future__ import annotations

from decimal import Decimal

import pytest

from nexassure.checks.expectations import evaluate, reduce_result
from nexassure.connectors.base import QueryResult
from nexassure.core.enums import ResultShape
from nexassure.core.models import Expectation
from nexassure.exceptions import ExpectationError


def result(columns, rows) -> QueryResult:
    return QueryResult(columns=list(columns), rows=[tuple(r) for r in rows])


class TestReduction:
    def test_scalar_takes_the_first_cell(self):
        assert reduce_result(result(["n"], [[42], [7]]), ResultShape.SCALAR) == 42

    def test_row_count_counts_rows(self):
        assert reduce_result(result(["n"], [[1], [2], [3]]), ResultShape.ROW_COUNT) == 3

    def test_column_collects_the_first_column(self):
        reduced = reduce_result(result(["a", "b"], [[1, 9], [2, 8]]), ResultShape.COLUMN)
        assert reduced == [1, 2]

    def test_table_returns_every_row(self):
        reduced = reduce_result(result(["a", "b"], [[1, 2], [3, 4]]), ResultShape.TABLE)
        assert reduced == [[1, 2], [3, 4]]


class TestComparison:
    def test_equality_tolerates_decimal_int_and_float(self):
        expectation = Expectation(operator="eq", value=3)
        for observed in (3, 3.0, Decimal("3")):
            passed, _, _ = evaluate(result(["n"], [[observed]]), expectation)
            assert passed, observed

    def test_ordering(self):
        expectation = Expectation(operator="lte", value=100)
        assert evaluate(result(["n"], [[99]]), expectation)[0]
        assert not evaluate(result(["n"], [[101]]), expectation)[0]

    def test_between_is_inclusive(self):
        expectation = Expectation(operator="between", value=[10, 20])
        assert evaluate(result(["n"], [[10]]), expectation)[0]
        assert evaluate(result(["n"], [[20]]), expectation)[0]
        assert not evaluate(result(["n"], [[21]]), expectation)[0]

    def test_approx_honours_absolute_tolerance(self):
        expectation = Expectation(operator="approx", value=100.0, tolerance=0.5)
        assert evaluate(result(["n"], [[100.4]]), expectation)[0]
        assert not evaluate(result(["n"], [[101.0]]), expectation)[0]

    def test_approx_honours_relative_tolerance(self):
        expectation = Expectation(operator="approx", value=1000.0, relative_tolerance=0.01)
        assert evaluate(result(["n"], [[1005.0]]), expectation)[0]
        assert not evaluate(result(["n"], [[1200.0]]), expectation)[0]

    def test_membership(self):
        expectation = Expectation(operator="in", value=["a", "b"])
        assert evaluate(result(["s"], [["a"]]), expectation)[0]
        assert not evaluate(result(["s"], [["z"]]), expectation)[0]

    def test_regex(self):
        expectation = Expectation(operator="matches", value=r"^\d{4}-\d{2}$")
        assert evaluate(result(["s"], [["2026-08"]]), expectation)[0]
        assert not evaluate(result(["s"], [["august"]]), expectation)[0]

    def test_invalid_regex_reports_clearly(self):
        with pytest.raises(ExpectationError, match="Invalid regex"):
            evaluate(result(["s"], [["x"]]), Expectation(operator="matches", value="[unclosed"))

    def test_set_equals_ignores_order(self):
        expectation = Expectation(shape="column", operator="set_equals", value=["b", "a"])
        assert evaluate(result(["s"], [["a"], ["b"]]), expectation)[0]

    def test_rows_equal_ignores_row_order_by_default(self):
        expectation = Expectation(shape="table", operator="rows_equal", value=[[2, "b"], [1, "a"]])
        assert evaluate(result(["n", "s"], [[1, "a"], [2, "b"]]), expectation)[0]

    def test_rows_equal_can_require_order(self):
        expectation = Expectation(
            shape="table",
            operator="rows_equal",
            value=[[2, "b"], [1, "a"]],
            ignore_row_order=False,
        )
        assert not evaluate(result(["n", "s"], [[1, "a"], [2, "b"]]), expectation)[0]

    def test_ignore_case(self):
        expectation = Expectation(operator="eq", value="SHIPPED", ignore_case=True)
        assert evaluate(result(["s"], [["shipped"]]), expectation)[0]


class TestEmptyResults:
    def test_a_scalar_expectation_over_no_rows_fails_with_a_clear_reason(self):
        passed, observed, explanation = evaluate(
            result(["n"], []), Expectation(operator="eq", value=0)
        )
        assert not passed
        assert observed is None
        assert "no rows" in explanation

    def test_empty_operator_accepts_no_rows(self):
        assert evaluate(result(["n"], []), Expectation(operator="empty"))[0]

    def test_not_empty_rejects_no_rows(self):
        assert not evaluate(result(["n"], []), Expectation(operator="not_empty"))[0]

    def test_row_count_zero_over_no_rows(self):
        expectation = Expectation(shape="row_count", operator="eq", value=0)
        assert evaluate(result(["n"], []), expectation)[0]
