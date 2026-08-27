# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Custom SQL checks.

This is the escape hatch that makes NexAssure useful for business rules no
built-in check can express. A user supplies three things:

* a **description** of what the rule means,
* a **query**, and
* the **expected output**.

.. code-block:: yaml

    - name: revenue_reconciles_with_ledger
      type: custom_sql
      description: Daily revenue must match the finance ledger to within a cent.
      query: |
        SELECT ABS(SUM(o.amount) - SUM(l.amount))
        FROM orders o JOIN ledger l ON o.day = l.day
        WHERE o.day = CURRENT_DATE - 1
      expect:
        operator: lte
        value: 0.01

The dbt-style convention - "a test passes when the query returns no rows" - is
available as its own type, ``sql_returns_no_rows``, so nobody has to write an
``expect`` block for the most common case.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..core.enums import Operator, ResultShape
from ..core.models import Expectation
from ..exceptions import CheckExecutionError
from .base import Check, CheckContext, Outcome, register_check
from .expectations import evaluate as evaluate_expectation


class _SQLCheckBase(Check):
    """Shared plumbing for checks that run user-supplied SQL."""

    requires_dataset: ClassVar[bool] = False
    supported_params: ClassVar[tuple[str, ...]] = ("bind", "max_rows", "allow_write")

    def _render_query(self, ctx: CheckContext) -> str:
        if not self.spec.query or not self.spec.query.strip():
            raise CheckExecutionError(
                f"Check {self.spec.name!r} of type {self.type_name!r} requires a 'query'",
                check=self.spec.name,
            )
        query = self.spec.query.strip().rstrip(";")

        # ``{{ table }}`` lets one query be reused against several datasets.
        if self.spec.dataset is not None:
            qualified = ctx.dialect.qualify(self.spec.dataset)
            query = query.replace("{{ table }}", qualified).replace("{{table}}", qualified)
        if self.spec.column:
            quoted = ctx.dialect.quote(self.spec.column)
            query = query.replace("{{ column }}", quoted).replace("{{column}}", quoted)
        return query

    def _execute(self, ctx: CheckContext, max_rows: int):
        query = self._render_query(ctx)
        binds: dict[str, Any] = self.param("bind") or {}
        # allow_write is an explicit, per-check opt-out of the read-only guard.
        # It still cannot override a database role that lacks write permission.
        enforce = not bool(self.param("allow_write", False))
        return query, ctx.connector.execute(
            query, binds, max_rows=max_rows, enforce_readonly=enforce
        )


@register_check
class CustomSQLCheck(_SQLCheckBase):
    """Run a query and compare its output to an expectation."""

    type_name: ClassVar[str] = "custom_sql"
    summary: ClassVar[str] = "Run a SQL query and compare the result to an expected output"

    #: When no ``expect`` block is given, assume the query returns no rows.
    DEFAULT_EXPECTATION: ClassVar[Expectation] = Expectation(
        shape=ResultShape.ROW_COUNT, operator=Operator.EQ, value=0
    )

    def evaluate(self, ctx: CheckContext) -> Outcome:
        expectation = self.spec.expect or self.DEFAULT_EXPECTATION
        # TABLE and COLUMN comparisons need the whole result; scalars do not.
        default_rows = (
            10_000 if expectation.shape in (ResultShape.TABLE, ResultShape.COLUMN) else 1_000
        )
        max_rows = int(self.param("max_rows", default_rows))

        query, result = self._execute(ctx, max_rows)
        if result.truncated:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} returned more than {max_rows:,} rows. "
                "Aggregate in SQL, or raise the 'max_rows' param.",
                check=self.spec.name,
            )

        passed, observed, explanation = evaluate_expectation(result, expectation)

        samples: list[dict[str, Any]] = []
        if not passed and self.spec.sample_limit and result.rows:
            samples = result.dicts()[: self.spec.sample_limit]

        return Outcome(
            passed=passed,
            observed=observed,
            expected=expectation.value
            if expectation.value is not None
            else expectation.operator.value,
            rows_scanned=result.row_count,
            rows_failed=None if passed else (result.row_count or 1),
            sample_rows=samples,
            query=query,
            message=(
                f"Expectation met: {explanation}"
                if passed
                else f"Expectation not met: {explanation}"
            ),
        )


@register_check
class SQLReturnsNoRowsCheck(_SQLCheckBase):
    """The query must return zero rows.

    Write the query so that every returned row is a violation, and the rows
    themselves become the failure evidence.
    """

    type_name: ClassVar[str] = "sql_returns_no_rows"
    summary: ClassVar[str] = "Query returns no rows - each returned row is a violation"

    def evaluate(self, ctx: CheckContext) -> Outcome:
        max_rows = int(self.param("max_rows", 1_000))
        query, result = self._execute(ctx, max_rows)
        failing = result.row_count
        approx = "+" if result.truncated else ""

        return Outcome(
            passed=failing == 0,
            observed=f"{failing}{approx}",
            expected=0,
            rows_scanned=failing,
            rows_failed=failing,
            sample_rows=result.dicts()[: self.spec.sample_limit],
            query=query,
            message=(
                f"Query returned {failing:,}{approx} violating row(s)"
                if failing
                else "Query returned no rows"
            ),
        )


@register_check
class SQLReturnsRowsCheck(_SQLCheckBase):
    """The query must return at least one row.

    The inverse assertion, for rules shaped as "this thing should exist".
    """

    type_name: ClassVar[str] = "sql_returns_rows"
    summary: ClassVar[str] = "Query returns at least one row"

    def evaluate(self, ctx: CheckContext) -> Outcome:
        minimum = int(self.param("min_rows", 1))
        query, result = self._execute(ctx, max(minimum, 100))
        found = result.row_count

        return Outcome(
            passed=found >= minimum,
            observed=found,
            expected=f">= {minimum}",
            rows_scanned=found,
            rows_failed=0 if found >= minimum else minimum - found,
            query=query,
            message=f"Query returned {found:,} row(s), expected at least {minimum}",
        )


# ``min_rows`` only applies to the type above, so it is declared here rather
# than on the shared base.
SQLReturnsRowsCheck.supported_params = (*_SQLCheckBase.supported_params, "min_rows")


@register_check
class CompareQueriesCheck(_SQLCheckBase):
    """Two queries must agree.

    The workhorse of migration and reconciliation testing: point one query at
    the source and one at the target, and assert they return the same thing.
    """

    type_name: ClassVar[str] = "compare_queries"
    summary: ClassVar[str] = "Two queries return the same result"
    supported_params: ClassVar[tuple[str, ...]] = (
        "bind",
        "max_rows",
        "allow_write",
        "other_query",
        "other_connection",
    )

    def evaluate(self, ctx: CheckContext) -> Outcome:
        other_query = str(self.required_param("other_query")).strip().rstrip(";")
        max_rows = int(self.param("max_rows", 10_000))

        query, left = self._execute(ctx, max_rows)
        right = ctx.connector.execute(other_query, self.param("bind") or {}, max_rows=max_rows)

        if left.truncated or right.truncated:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} compared more than {max_rows:,} rows. "
                "Aggregate in SQL, or raise the 'max_rows' param.",
                check=self.spec.name,
            )

        expectation = self.spec.expect or Expectation(
            shape=ResultShape.TABLE, operator=Operator.ROWS_EQUAL, value=[]
        )
        expectation = expectation.model_copy(
            update={
                "value": [list(row) for row in right.rows],
                "operator": Operator.ROWS_EQUAL,
                "shape": ResultShape.TABLE,
            }
        )
        passed, _observed, explanation = evaluate_expectation(left, expectation)

        difference = abs(left.row_count - right.row_count)
        return Outcome(
            passed=passed,
            observed={"left_rows": left.row_count, "right_rows": right.row_count},
            expected="identical result sets",
            rows_scanned=max(left.row_count, right.row_count),
            rows_failed=0 if passed else max(difference, 1),
            sample_rows=left.dicts()[: self.spec.sample_limit] if not passed else [],
            query=f"-- left\n{query}\n-- right\n{other_query}",
            message=(
                f"Result sets match ({left.row_count:,} rows)"
                if passed
                else f"Result sets differ: {explanation} "
                f"(left {left.row_count:,} rows, right {right.row_count:,} rows)"
            ),
        )


def register() -> None:
    """Entry-point hook. Importing this module already registers everything."""
    return None
