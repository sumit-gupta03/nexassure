# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Built-in check types.

These cover the checks that show up in nearly every data quality suite:
completeness (nulls, blanks), uniqueness (duplicates, primary keys), validity
(accepted values, ranges, patterns, lengths), volume (row counts), timeliness
(freshness) and consistency (referential integrity, schema drift).

Most are three lines because :class:`~nexassure.checks.base.RowPredicateCheck`
already knows how to count and sample failing rows. The ones that need a
GROUP BY or a second table implement :meth:`Check.evaluate` directly.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..core.models import TableRef
from ..exceptions import CheckExecutionError
from .base import Check, CheckContext, Outcome, RowPredicateCheck, register_check

# --------------------------------------------------------------------------- #
# Completeness
# --------------------------------------------------------------------------- #


@register_check
class NotNullCheck(RowPredicateCheck):
    """Every value in the column is present."""

    type_name: ClassVar[str] = "not_null"
    summary: ClassVar[str] = "Column contains no NULL values"
    requires_column: ClassVar[bool] = True
    violation_noun: ClassVar[str] = "NULL values"

    def failing_predicate(self, ctx: CheckContext) -> str:
        return f"{self.col(ctx)} IS NULL"


@register_check
class NotBlankCheck(RowPredicateCheck):
    """String column has no empty or whitespace-only values.

    NULLs are treated as blank too, since a caller asking for "no blanks"
    almost never wants NULLs to slip through.
    """

    type_name: ClassVar[str] = "not_blank"
    summary: ClassVar[str] = "String column has no empty or whitespace-only values"
    requires_column: ClassVar[bool] = True
    violation_noun: ClassVar[str] = "blank values"

    def failing_predicate(self, ctx: CheckContext) -> str:
        column = self.col(ctx)
        trimmed = ctx.dialect.trim(ctx.dialect.cast_varchar(column))
        return f"({column} IS NULL OR {trimmed} = '')"


@register_check
class CompletenessCheck(Check):
    """Non-NULL ratio meets a minimum.

    Where ``not_null`` is all-or-nothing, this expresses "at least 95% of rows
    must have a value", which is how completeness SLAs are usually written.
    """

    type_name: ClassVar[str] = "completeness"
    summary: ClassVar[str] = "Fraction of non-NULL values is at or above min_ratio"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("min_ratio",)

    def evaluate(self, ctx: CheckContext) -> Outcome:
        min_ratio = float(self.param("min_ratio", 1.0))
        column = self.col(ctx)
        sql = (
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS nulls "
            f"FROM {self.qualified_table(ctx)}{self.where_clause()}"
        )
        row = ctx.connector.execute(sql, max_rows=1).first()
        total = int(row[0] or 0) if row else 0
        nulls = int(row[1] or 0) if row and row[1] is not None else 0
        # An empty table is vacuously complete; failing it would fire on every
        # newly created partition before the first load.
        ratio = 1.0 if total == 0 else (total - nulls) / total

        return Outcome(
            passed=ratio >= min_ratio,
            observed=round(ratio, 6),
            expected=f">= {min_ratio}",
            rows_scanned=total,
            rows_failed=nulls,
            query=sql,
            message=f"Completeness {ratio:.2%} against a minimum of {min_ratio:.2%}",
        )


# --------------------------------------------------------------------------- #
# Uniqueness
# --------------------------------------------------------------------------- #


@register_check
class UniqueCheck(Check):
    """No duplicate values in a column, or in a combination of columns.

    Reports both numbers people ask for: how many distinct values are
    duplicated, and how many surplus rows would have to be deleted.
    """

    type_name: ClassVar[str] = "unique"
    summary: ClassVar[str] = "Column (or column combination) has no duplicate values"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("ignore_nulls",)

    def evaluate(self, ctx: CheckContext) -> Outcome:
        ignore_nulls = bool(self.param("ignore_nulls", True))
        columns = [ctx.dialect.quote(c) for c in self.spec.columns]
        grouped = ", ".join(columns)
        table = self.qualified_table(ctx)

        # NULL never equals NULL in SQL, so NULL rows can never be duplicates of
        # each other. Excluding them keeps the scanned-row denominator honest.
        null_filter = " AND ".join(f"{c} IS NOT NULL" for c in columns) if ignore_nulls else None
        scope = self.where_clause(null_filter)

        sql = (
            f"SELECT COUNT(*) AS duplicated_values, "
            f"COALESCE(SUM(group_size - 1), 0) AS surplus_rows, "
            f"COALESCE(SUM(group_size), 0) AS rows_in_duplicate_groups "
            f"FROM (SELECT {grouped}, COUNT(*) AS group_size "
            f"FROM {table}{scope} GROUP BY {grouped} HAVING COUNT(*) > 1) AS dup_groups"
        )
        row = ctx.connector.execute(sql, max_rows=1).first()
        duplicated_values = int(row[0] or 0) if row else 0
        surplus = int(row[1] or 0) if row and row[1] is not None else 0

        total = ctx.connector.count_rows(self.spec.dataset, self._raw_scope(null_filter))

        samples: list[dict[str, Any]] = []
        if duplicated_values and self.spec.sample_limit:
            sample_sql = ctx.dialect.limit(
                f"SELECT {grouped}, COUNT(*) AS occurrences FROM {table}{scope} "
                f"GROUP BY {grouped} HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC",
                self.spec.sample_limit,
            )
            try:
                samples = ctx.connector.execute(sample_sql, max_rows=self.spec.sample_limit).dicts()
            except Exception:
                samples = []

        label = ", ".join(self.spec.columns)
        return Outcome(
            passed=duplicated_values == 0,
            observed=duplicated_values,
            expected=0,
            rows_scanned=total,
            rows_failed=surplus,
            sample_rows=samples,
            query=sql,
            message=(
                f"{duplicated_values:,} duplicated value(s) of ({label}) "
                f"across {surplus:,} surplus row(s)"
                if duplicated_values
                else f"({label}) is unique across {total:,} rows"
            ),
        )

    def _raw_scope(self, null_filter: str | None) -> str | None:
        parts = [p for p in (self.spec.where, null_filter) if p]
        return " AND ".join(f"({p})" for p in parts) if parts else None


@register_check
class PrimaryKeyCheck(UniqueCheck):
    """Column combination is a valid primary key: unique *and* never NULL.

    Runs uniqueness with NULLs included, so a NULL key is caught rather than
    quietly excluded from the duplicate scan.
    """

    type_name: ClassVar[str] = "primary_key"
    summary: ClassVar[str] = "Column combination is unique and never NULL"
    supported_params: ClassVar[tuple[str, ...]] = ()

    def evaluate(self, ctx: CheckContext) -> Outcome:
        self.spec.params["ignore_nulls"] = False
        uniqueness = super().evaluate(ctx)

        columns = [ctx.dialect.quote(c) for c in self.spec.columns]
        any_null = " OR ".join(f"{c} IS NULL" for c in columns)
        null_sql = f"SELECT COUNT(*) FROM {self.qualified_table(ctx)}{self.where_clause(any_null)}"
        null_rows = int(ctx.connector.scalar(null_sql) or 0)

        total_failed = (uniqueness.rows_failed or 0) + null_rows
        label = ", ".join(self.spec.columns)
        return Outcome(
            passed=uniqueness.passed and null_rows == 0,
            observed={"duplicate_values": uniqueness.observed, "null_rows": null_rows},
            expected={"duplicate_values": 0, "null_rows": 0},
            rows_scanned=uniqueness.rows_scanned,
            rows_failed=total_failed,
            sample_rows=uniqueness.sample_rows,
            query=uniqueness.query,
            message=(
                f"({label}) is a valid primary key"
                if uniqueness.passed and null_rows == 0
                else f"({label}) violates the key: {uniqueness.observed} duplicated value(s), "
                f"{null_rows:,} NULL row(s)"
            ),
        )


@register_check
class NoDuplicateRowsCheck(Check):
    """The table contains no fully duplicated rows.

    Column list comes from catalog introspection, so the check keeps working
    when the table gains a column.
    """

    type_name: ClassVar[str] = "no_duplicate_rows"
    summary: ClassVar[str] = "Table has no completely duplicated rows"
    supported_params: ClassVar[tuple[str, ...]] = ("exclude_columns",)

    def evaluate(self, ctx: CheckContext) -> Outcome:
        assert self.spec.dataset is not None
        excluded = {c.lower() for c in (self.param("exclude_columns") or [])}
        dataset = ctx.connector.describe_table(self.spec.dataset)
        names = [c.name for c in dataset.columns if c.name.lower() not in excluded]
        if not names:
            raise CheckExecutionError(
                f"No columns left to compare on {self.spec.dataset.fqn} "
                "after applying exclude_columns",
                check=self.spec.name,
            )

        grouped = ", ".join(ctx.dialect.quote(c) for c in names)
        table = self.qualified_table(ctx)
        scope = self.where_clause()
        sql = (
            f"SELECT COUNT(*) AS duplicated_rows, COALESCE(SUM(group_size - 1), 0) AS surplus_rows "
            f"FROM (SELECT {grouped}, COUNT(*) AS group_size "
            f"FROM {table}{scope} GROUP BY {grouped} HAVING COUNT(*) > 1) AS dup_rows"
        )
        row = ctx.connector.execute(sql, max_rows=1).first()
        duplicated = int(row[0] or 0) if row else 0
        surplus = int(row[1] or 0) if row and row[1] is not None else 0
        total = ctx.connector.count_rows(self.spec.dataset, self.spec.where)

        return Outcome(
            passed=duplicated == 0,
            observed=duplicated,
            expected=0,
            rows_scanned=total,
            rows_failed=surplus,
            query=sql,
            message=(
                f"{duplicated:,} duplicated row pattern(s), {surplus:,} surplus row(s)"
                if duplicated
                else f"No duplicate rows across {total:,} rows"
            ),
        )


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #


@register_check
class RowCountCheck(Check):
    """Row count sits inside an expected band.

    Accepts ``min``, ``max``, or ``equals``. With none of them, it degrades to
    "the table must not be empty", which is the useful default.
    """

    type_name: ClassVar[str] = "row_count"
    summary: ClassVar[str] = "Row count is within min/max, or equals an exact value"
    supported_params: ClassVar[tuple[str, ...]] = ("min", "max", "equals")

    def evaluate(self, ctx: CheckContext) -> Outcome:
        assert self.spec.dataset is not None
        minimum = self.param("min")
        maximum = self.param("max")
        exact = self.param("equals")

        sql = f"SELECT COUNT(*) FROM {self.qualified_table(ctx)}{self.where_clause()}"
        count = int(ctx.connector.scalar(sql) or 0)

        if exact is not None:
            passed = count == int(exact)
            expected: Any = int(exact)
            detail = f"exactly {int(exact):,}"
        elif minimum is None and maximum is None:
            passed = count > 0
            expected = "> 0"
            detail = "more than 0"
        else:
            passed = True
            bounds = []
            if minimum is not None:
                passed = passed and count >= int(minimum)
                bounds.append(f">= {int(minimum):,}")
            if maximum is not None:
                passed = passed and count <= int(maximum)
                bounds.append(f"<= {int(maximum):,}")
            expected = " and ".join(bounds)
            detail = " and ".join(bounds)

        return Outcome(
            passed=passed,
            observed=count,
            expected=expected,
            rows_scanned=count,
            rows_failed=0 if passed else count,
            query=sql,
            message=f"Row count {count:,}, expected {detail}",
        )


@register_check
class NotEmptyCheck(RowCountCheck):
    """The table has at least one row."""

    type_name: ClassVar[str] = "not_empty"
    summary: ClassVar[str] = "Table contains at least one row"
    supported_params: ClassVar[tuple[str, ...]] = ()

    def evaluate(self, ctx: CheckContext) -> Outcome:
        self.spec.params.setdefault("min", 1)
        return super().evaluate(ctx)


# --------------------------------------------------------------------------- #
# Validity
# --------------------------------------------------------------------------- #


@register_check
class AcceptedValuesCheck(RowPredicateCheck):
    """Every value belongs to an allowed set."""

    type_name: ClassVar[str] = "accepted_values"
    summary: ClassVar[str] = "Column values all come from an allowed set"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("values", "ignore_nulls")
    violation_noun: ClassVar[str] = "disallowed values"

    def failing_predicate(self, ctx: CheckContext) -> str:
        values = self.required_param("values")
        if not isinstance(values, (list, tuple)) or not values:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} needs a non-empty 'values' list", check=self.spec.name
            )
        rendered = ", ".join(_literal(ctx, v) for v in values)
        column = self.col(ctx)
        predicate = f"{column} NOT IN ({rendered})"
        if self.param("ignore_nulls", True):
            # NOT IN is already NULL-safe in the sense that it never matches
            # NULL, so this is about intent, not correctness.
            return predicate
        return f"({predicate} OR {column} IS NULL)"


@register_check
class RejectedValuesCheck(RowPredicateCheck):
    """No value appears in a forbidden set."""

    type_name: ClassVar[str] = "rejected_values"
    summary: ClassVar[str] = "Column contains none of the forbidden values"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("values",)
    violation_noun: ClassVar[str] = "forbidden values"

    def failing_predicate(self, ctx: CheckContext) -> str:
        values = self.required_param("values")
        rendered = ", ".join(_literal(ctx, v) for v in values)
        return f"{self.col(ctx)} IN ({rendered})"


@register_check
class RangeCheck(RowPredicateCheck):
    """Numeric or date values fall inside a bound.

    ``inclusive`` (default true) decides whether the endpoints themselves pass.
    """

    type_name: ClassVar[str] = "range"
    summary: ClassVar[str] = "Values fall between min and max"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("min", "max", "inclusive")
    violation_noun: ClassVar[str] = "out-of-range values"

    def failing_predicate(self, ctx: CheckContext) -> str:
        minimum, maximum = self.param("min"), self.param("max")
        if minimum is None and maximum is None:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} needs at least one of 'min' or 'max'",
                check=self.spec.name,
            )
        inclusive = bool(self.param("inclusive", True))
        column = self.col(ctx)
        clauses = []
        if minimum is not None:
            clauses.append(f"{column} {'<' if inclusive else '<='} {_literal(ctx, minimum)}")
        if maximum is not None:
            clauses.append(f"{column} {'>' if inclusive else '>='} {_literal(ctx, maximum)}")
        # NULLs are out of scope here; use not_null to assert presence.
        return f"({column} IS NOT NULL AND ({' OR '.join(clauses)}))"


@register_check
class RegexCheck(RowPredicateCheck):
    """Values match a regular expression.

    SQL Server and Synapse have no regex engine, so the pattern is handed to
    ``LIKE`` there. Anchored wildcard patterns still work; richer expressions
    should be written as a ``custom_sql`` check on those engines.
    """

    type_name: ClassVar[str] = "regex"
    summary: ClassVar[str] = "String values match a regular expression"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("pattern", "ignore_nulls", "negate")
    violation_noun: ClassVar[str] = "non-matching values"

    def failing_predicate(self, ctx: CheckContext) -> str:
        pattern = str(self.required_param("pattern"))
        column = self.col(ctx)
        matches = ctx.dialect.regexp_match(column, ctx.dialect.string_literal(pattern))
        # Negate flips the check into "must NOT match", used for banned formats.
        violation = matches if self.param("negate", False) else f"NOT ({matches})"
        if self.param("ignore_nulls", True):
            return f"({column} IS NOT NULL AND {violation})"
        return f"({column} IS NULL OR {violation})"


@register_check
class LengthCheck(RowPredicateCheck):
    """String length falls inside a bound."""

    type_name: ClassVar[str] = "length"
    summary: ClassVar[str] = "String length is between min_length and max_length"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("min_length", "max_length", "equals")
    violation_noun: ClassVar[str] = "values with an unexpected length"

    def failing_predicate(self, ctx: CheckContext) -> str:
        minimum = self.param("min_length")
        maximum = self.param("max_length")
        exact = self.param("equals")
        if minimum is None and maximum is None and exact is None:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} needs min_length, max_length or equals",
                check=self.spec.name,
            )
        column = self.col(ctx)
        length = ctx.dialect.length(ctx.dialect.cast_varchar(column))
        clauses = []
        if exact is not None:
            clauses.append(f"{length} <> {int(exact)}")
        if minimum is not None:
            clauses.append(f"{length} < {int(minimum)}")
        if maximum is not None:
            clauses.append(f"{length} > {int(maximum)}")
        return f"({column} IS NOT NULL AND ({' OR '.join(clauses)}))"


# --------------------------------------------------------------------------- #
# Timeliness
# --------------------------------------------------------------------------- #


@register_check
class FreshnessCheck(Check):
    """The newest row is recent enough.

    The staleness window is expressed in hours against the warehouse clock, not
    the client clock, so a runner in a different timezone cannot skew it.
    """

    type_name: ClassVar[str] = "freshness"
    summary: ClassVar[str] = "Newest timestamp is within max_age_hours"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("max_age_hours", "max_age_minutes")

    def evaluate(self, ctx: CheckContext) -> Outcome:
        hours = self.param("max_age_hours")
        minutes = self.param("max_age_minutes")
        if hours is None and minutes is None:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} needs 'max_age_hours' or 'max_age_minutes'",
                check=self.spec.name,
            )
        max_age_hours = float(hours) if hours is not None else float(minutes) / 60.0

        column = self.col(ctx)
        age_expr = ctx.dialect.hours_between(ctx.dialect.current_timestamp(), f"MAX({column})")
        sql = (
            f"SELECT MAX({column}) AS newest, {age_expr} AS age_hours "
            f"FROM {self.qualified_table(ctx)}{self.where_clause()}"
        )
        row = ctx.connector.execute(sql, max_rows=1).first()
        newest = row[0] if row else None
        age = float(row[1]) if row and row[1] is not None else None

        if newest is None or age is None:
            return Outcome(
                passed=False,
                observed=None,
                expected=f"<= {max_age_hours} hours old",
                query=sql,
                message="No timestamps found, so freshness cannot be established",
            )

        return Outcome(
            passed=age <= max_age_hours,
            observed={"newest": str(newest), "age_hours": round(age, 3)},
            expected=f"<= {max_age_hours} hours old",
            query=sql,
            message=(f"Newest row is {age:.2f}h old (limit {max_age_hours}h), timestamp {newest}"),
        )


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #


@register_check
class ReferentialIntegrityCheck(Check):
    """Every value exists in a parent table (a foreign key that is not enforced).

    Warehouses rarely enforce foreign keys, so orphaned facts are one of the
    most common real defects this framework is used to catch.
    """

    type_name: ClassVar[str] = "referential_integrity"
    summary: ClassVar[str] = "Column values all exist in a parent table column"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("to", "field", "ignore_nulls")

    def evaluate(self, ctx: CheckContext) -> Outcome:
        parent_raw = self.required_param("to")
        parent_column = self.required_param("field")
        ignore_nulls = bool(self.param("ignore_nulls", True))

        parent = TableRef.parse(str(parent_raw))
        if parent.db_schema is None and self.spec.dataset is not None:
            parent.db_schema = self.spec.dataset.db_schema
        if parent.database is None and self.spec.dataset is not None:
            parent.database = self.spec.dataset.database

        child_table = self.qualified_table(ctx)
        parent_table = ctx.dialect.qualify(parent)
        child_col = self.col(ctx)
        parent_col = ctx.dialect.quote(str(parent_column))

        # NOT EXISTS beats NOT IN here: NOT IN silently returns zero rows when
        # the parent column contains a single NULL.
        orphan_predicate = (
            f"NOT EXISTS (SELECT 1 FROM {parent_table} AS _na_parent "
            f"WHERE _na_parent.{parent_col} = _na_child.{child_col})"
        )
        null_guard = f"_na_child.{child_col} IS NOT NULL" if ignore_nulls else "1 = 1"
        extra = f" AND ({self.spec.where})" if self.spec.where else ""

        sql = (
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN {orphan_predicate} THEN 1 ELSE 0 END) AS orphans "
            f"FROM {child_table} AS _na_child WHERE {null_guard}{extra}"
        )
        row = ctx.connector.execute(sql, max_rows=1).first()
        total = int(row[0] or 0) if row else 0
        orphans = int(row[1] or 0) if row and row[1] is not None else 0

        samples: list[dict[str, Any]] = []
        if orphans and self.spec.sample_limit:
            sample_sql = ctx.dialect.limit(
                f"SELECT DISTINCT _na_child.{child_col} AS orphan_value "
                f"FROM {child_table} AS _na_child "
                f"WHERE {null_guard}{extra} AND {orphan_predicate}",
                self.spec.sample_limit,
            )
            try:
                samples = ctx.connector.execute(sample_sql, max_rows=self.spec.sample_limit).dicts()
            except Exception:
                samples = []

        return Outcome(
            passed=orphans == 0,
            observed=orphans,
            expected=0,
            rows_scanned=total,
            rows_failed=orphans,
            sample_rows=samples,
            query=sql,
            message=(
                f"{orphans:,} orphaned row(s) with no match in {parent.fqn}.{parent_column}"
                if orphans
                else f"All {total:,} values resolve in {parent.fqn}.{parent_column}"
            ),
        )


@register_check
class SchemaCheck(Check):
    """The table exposes the columns the contract promises.

    Catches the two failure modes that break downstream jobs silently: a column
    disappearing, and a column changing type. Extra columns are tolerated unless
    ``strict`` is set, because additive changes are usually safe.
    """

    type_name: ClassVar[str] = "schema"
    summary: ClassVar[str] = "Table has the expected columns, and optionally the expected types"
    supported_params: ClassVar[tuple[str, ...]] = ("columns", "strict", "check_types")

    def evaluate(self, ctx: CheckContext) -> Outcome:
        assert self.spec.dataset is not None
        expected_spec = self.required_param("columns")
        strict = bool(self.param("strict", False))
        check_types = bool(self.param("check_types", True))

        dataset = ctx.connector.describe_table(self.spec.dataset)
        actual = {c.name.lower(): c for c in dataset.columns}

        # Accept both ["id", "name"] and [{name: id, type: integer}, ...].
        expected: dict[str, str | None] = {}
        for entry in expected_spec:
            if isinstance(entry, dict):
                expected[str(entry["name"]).lower()] = entry.get("type")
            else:
                expected[str(entry).lower()] = None

        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected)) if strict else []
        type_mismatches = []
        if check_types:
            for name, wanted in expected.items():
                if wanted and name in actual:
                    found = actual[name].data_type.lower()
                    if wanted.lower() not in found:
                        type_mismatches.append(
                            {"column": name, "expected": wanted, "actual": actual[name].data_type}
                        )

        problems = len(missing) + len(unexpected) + len(type_mismatches)
        parts = []
        if missing:
            parts.append(f"missing: {', '.join(missing)}")
        if unexpected:
            parts.append(f"unexpected: {', '.join(unexpected)}")
        if type_mismatches:
            parts.append(
                "type drift: "
                + ", ".join(
                    f"{m['column']} ({m['actual']} != {m['expected']})" for m in type_mismatches
                )
            )

        return Outcome(
            passed=problems == 0,
            observed={
                "missing": missing,
                "unexpected": unexpected,
                "type_mismatches": type_mismatches,
                "actual_columns": [c.name for c in dataset.columns],
            },
            expected=sorted(expected),
            rows_scanned=len(actual),
            rows_failed=problems,
            message=(
                "; ".join(parts)
                if parts
                else f"Schema matches: {len(expected)} expected column(s) present"
            ),
        )


@register_check
class ColumnExistsCheck(Check):
    """A named column is present on the table."""

    type_name: ClassVar[str] = "column_exists"
    summary: ClassVar[str] = "Named column exists on the table"
    requires_column: ClassVar[bool] = True

    def evaluate(self, ctx: CheckContext) -> Outcome:
        assert self.spec.dataset is not None
        dataset = ctx.connector.describe_table(self.spec.dataset)
        present = {c.name.lower() for c in dataset.columns}
        missing = [c for c in self.spec.columns if c.lower() not in present]
        return Outcome(
            passed=not missing,
            observed=sorted(present),
            expected=self.spec.columns,
            rows_failed=len(missing),
            message=(
                f"Missing column(s): {', '.join(missing)}"
                if missing
                else f"All {len(self.spec.columns)} column(s) present"
            ),
        )


# --------------------------------------------------------------------------- #
# Statistical
# --------------------------------------------------------------------------- #


@register_check
class AggregateCheck(Check):
    """An aggregate over a column sits inside a band.

    Covers the "average order value should be between 10 and 500" family
    without anyone writing SQL.
    """

    type_name: ClassVar[str] = "aggregate"
    summary: ClassVar[str] = "An aggregate (avg/sum/min/max/count_distinct) is within bounds"
    requires_column: ClassVar[bool] = True
    supported_params: ClassVar[tuple[str, ...]] = ("function", "min", "max", "equals")

    _FUNCTIONS: ClassVar[dict[str, str]] = {
        "avg": "AVG",
        "mean": "AVG",
        "sum": "SUM",
        "min": "MIN",
        "max": "MAX",
        "count": "COUNT",
        "count_distinct": "COUNT",
        "stddev": "STDDEV",
    }

    def evaluate(self, ctx: CheckContext) -> Outcome:
        function = str(self.param("function", "avg")).lower()
        if function not in self._FUNCTIONS:
            raise CheckExecutionError(
                f"Unsupported aggregate {function!r}. "
                f"Use one of: {', '.join(sorted(self._FUNCTIONS))}",
                check=self.spec.name,
            )

        column = self.col(ctx)
        if function == "count_distinct":
            expression = f"COUNT(DISTINCT {column})"
        elif function == "stddev":
            expression = ctx.dialect.stddev(column)
        else:
            expression = f"{self._FUNCTIONS[function]}({column})"

        sql = f"SELECT {expression} FROM {self.qualified_table(ctx)}{self.where_clause()}"
        raw = ctx.connector.scalar(sql)
        value = None if raw is None else float(raw)

        minimum, maximum, exact = self.param("min"), self.param("max"), self.param("equals")
        if value is None:
            return Outcome(
                passed=False,
                observed=None,
                expected="a value",
                query=sql,
                message=f"{function}({self.spec.column}) returned NULL",
            )

        passed = True
        bounds = []
        if exact is not None:
            passed = value == float(exact)
            bounds.append(f"= {exact}")
        if minimum is not None:
            passed = passed and value >= float(minimum)
            bounds.append(f">= {minimum}")
        if maximum is not None:
            passed = passed and value <= float(maximum)
            bounds.append(f"<= {maximum}")
        if not bounds:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} needs min, max or equals", check=self.spec.name
            )

        return Outcome(
            passed=passed,
            observed=round(value, 6),
            expected=" and ".join(bounds),
            query=sql,
            message=f"{function}({self.spec.column}) = {value:,.4f}, expected {' and '.join(bounds)}",
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _literal(ctx: CheckContext, value: Any) -> str:
    """Render a Python value as a SQL literal.

    Values here come from suite YAML, which is trusted config rather than user
    input, but strings are still quote-escaped so a value containing an
    apostrophe cannot break out of its literal.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return ctx.dialect.string_literal(str(value))


def register() -> None:
    """Entry-point hook. Importing this module already registers everything."""
    return None
