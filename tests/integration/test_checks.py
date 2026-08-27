# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Every built-in check executed against a real database.

These run on DuckDB, so they exercise generated SQL end to end rather than
asserting on strings. The fixture data in ``conftest.py`` carries one planted
defect per check family, so each test has something real to find.
"""

from __future__ import annotations

import pytest

from nexassure.checks.base import build_check
from nexassure.core.enums import CheckStatus, Severity
from nexassure.core.models import CheckSpec

pytestmark = pytest.mark.integration


def run(ctx, **kwargs):
    kwargs.setdefault("name", "t")
    return build_check(CheckSpec(**kwargs)).run(ctx)


# --------------------------------------------------------------------------- #
# Completeness
# --------------------------------------------------------------------------- #


class TestNotNull:
    def test_passes_on_a_fully_populated_column(self, ctx):
        result = run(ctx, type="not_null", dataset="main.customers", column="id")
        assert result.status is CheckStatus.PASSED
        assert result.rows_failed == 0
        assert result.rows_scanned == 7

    def test_fails_and_counts_the_nulls(self, ctx):
        result = run(ctx, type="not_null", dataset="main.customers", column="email")
        assert result.status is CheckStatus.FAILED
        assert result.rows_failed == 1
        assert result.failed_ratio == pytest.approx(1 / 7)

    def test_captures_sample_failing_rows(self, ctx):
        result = run(ctx, type="not_null", dataset="main.customers", column="email")
        assert result.sample_rows
        assert result.sample_rows[0]["email"] is None

    def test_where_narrows_the_scope(self, ctx):
        result = run(
            ctx,
            type="not_null",
            dataset="main.customers",
            column="email",
            where="region = 'namer'",
        )
        assert result.status is CheckStatus.PASSED
        assert result.rows_scanned == 2


class TestNotBlank:
    def test_treats_whitespace_and_null_as_blank(self, ctx):
        result = run(ctx, type="not_blank", dataset="main.customers", column="email")
        assert result.status is CheckStatus.FAILED
        assert result.rows_failed == 2  # one NULL plus one whitespace-only


class TestCompleteness:
    def test_passes_when_above_the_floor(self, ctx):
        result = run(
            ctx,
            type="completeness",
            dataset="main.customers",
            column="email",
            params={"min_ratio": 0.8},
        )
        assert result.status is CheckStatus.PASSED
        assert result.observed == pytest.approx(6 / 7)

    def test_fails_when_below_the_floor(self, ctx):
        result = run(
            ctx,
            type="completeness",
            dataset="main.customers",
            column="email",
            params={"min_ratio": 0.99},
        )
        assert result.status is CheckStatus.FAILED

    def test_an_empty_table_is_vacuously_complete(self, ctx):
        result = run(
            ctx,
            type="completeness",
            dataset="main.empty_table",
            column="label",
            params={"min_ratio": 1.0},
        )
        assert result.status is CheckStatus.PASSED


# --------------------------------------------------------------------------- #
# Uniqueness
# --------------------------------------------------------------------------- #


class TestUnique:
    def test_passes_on_a_unique_column(self, ctx):
        assert run(ctx, type="unique", dataset="main.orders", column="order_id").passed

    def test_fails_and_reports_both_counts(self, ctx):
        result = run(ctx, type="unique", dataset="main.customers", column="id")
        assert result.status is CheckStatus.FAILED
        assert result.observed == 1  # one duplicated value, c5
        assert result.rows_failed == 1  # one surplus row

    def test_reports_the_offending_values(self, ctx):
        result = run(ctx, type="unique", dataset="main.customers", column="id")
        assert any(row.get("id") == "c5" for row in result.sample_rows)

    def test_a_composite_key_can_be_unique_when_its_parts_are_not(self, ctx):
        result = run(ctx, type="unique", dataset="main.customers", columns=["id", "email"])
        assert result.status is CheckStatus.PASSED


class TestPrimaryKey:
    def test_fails_on_a_duplicated_key(self, ctx):
        result = run(ctx, type="primary_key", dataset="main.customers", column="id")
        assert result.status is CheckStatus.FAILED
        assert result.observed["null_rows"] == 0

    def test_passes_on_a_real_key(self, ctx):
        assert run(ctx, type="primary_key", dataset="main.clean_table", column="id").passed


class TestNoDuplicateRows:
    def test_passes_on_distinct_rows(self, ctx):
        assert run(ctx, type="no_duplicate_rows", dataset="main.clean_table").passed

    def test_excluding_a_column_can_reveal_duplicates(self, ctx):
        # c5 appears twice, differing only in email and signup_date.
        result = run(
            ctx,
            type="no_duplicate_rows",
            dataset="main.customers",
            params={"exclude_columns": ["email", "signup_date", "region"]},
        )
        assert result.status is CheckStatus.FAILED


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #


class TestRowCount:
    def test_min_bound(self, ctx):
        assert run(ctx, type="row_count", dataset="main.orders", params={"min": 5}).passed
        assert not run(ctx, type="row_count", dataset="main.orders", params={"min": 100}).passed

    def test_max_bound(self, ctx):
        assert not run(ctx, type="row_count", dataset="main.orders", params={"max": 3}).passed

    def test_exact(self, ctx):
        assert run(ctx, type="row_count", dataset="main.orders", params={"equals": 7}).passed

    def test_with_no_params_it_means_not_empty(self, ctx):
        assert run(ctx, type="row_count", dataset="main.orders").passed
        assert not run(ctx, type="row_count", dataset="main.empty_table").passed


class TestNotEmpty:
    def test_empty_table_fails(self, ctx):
        assert not run(ctx, type="not_empty", dataset="main.empty_table").passed


# --------------------------------------------------------------------------- #
# Validity
# --------------------------------------------------------------------------- #


class TestAcceptedValues:
    def test_catches_a_value_outside_the_set(self, ctx):
        result = run(
            ctx,
            type="accepted_values",
            dataset="main.customers",
            column="region",
            params={"values": ["emea", "namer", "apac"]},
        )
        assert result.status is CheckStatus.FAILED
        assert result.rows_failed == 1  # 'mars'

    def test_passes_when_the_set_is_complete(self, ctx):
        result = run(
            ctx,
            type="accepted_values",
            dataset="main.customers",
            column="region",
            params={"values": ["emea", "namer", "apac", "mars"]},
        )
        assert result.status is CheckStatus.PASSED

    def test_a_value_containing_a_quote_is_escaped_not_injected(self, ctx):
        result = run(
            ctx,
            type="accepted_values",
            dataset="main.customers",
            column="region",
            params={"values": ["emea", "namer", "apac", "mars", "O'Brien"]},
        )
        assert result.status is CheckStatus.PASSED


class TestRejectedValues:
    def test_flags_a_forbidden_value(self, ctx):
        result = run(
            ctx,
            type="rejected_values",
            dataset="main.customers",
            column="region",
            params={"values": ["mars"]},
        )
        assert result.status is CheckStatus.FAILED
        assert result.rows_failed == 1


class TestRange:
    def test_catches_a_negative_value(self, ctx):
        result = run(ctx, type="range", dataset="main.orders", column="total", params={"min": 0})
        assert result.status is CheckStatus.FAILED
        assert result.rows_failed == 1

    def test_passes_inside_the_band(self, ctx):
        result = run(
            ctx,
            type="range",
            dataset="main.orders",
            column="total",
            params={"min": -100, "max": 1000},
        )
        assert result.status is CheckStatus.PASSED

    def test_exclusive_bounds_reject_the_endpoints(self, ctx):
        result = run(
            ctx,
            type="range",
            dataset="main.clean_table",
            column="id",
            params={"min": 1, "max": 3, "inclusive": False},
        )
        assert result.status is CheckStatus.FAILED


class TestRegex:
    def test_flags_values_that_do_not_match(self, ctx):
        result = run(
            ctx,
            type="regex",
            dataset="main.customers",
            column="email",
            params={"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
        )
        assert result.status is CheckStatus.FAILED
        assert result.rows_failed == 1  # the whitespace-only email

    def test_negate_inverts_the_rule(self, ctx):
        result = run(
            ctx,
            type="regex",
            dataset="main.customers",
            column="region",
            params={"pattern": "mars", "negate": True},
        )
        assert result.status is CheckStatus.FAILED


class TestLength:
    def test_max_length(self, ctx):
        result = run(
            ctx,
            type="length",
            dataset="main.clean_table",
            column="label",
            params={"max_length": 4},
        )
        assert result.status is CheckStatus.FAILED  # alpha and gamma are 5 chars

    def test_min_length(self, ctx):
        result = run(
            ctx,
            type="length",
            dataset="main.clean_table",
            column="label",
            params={"min_length": 4},
        )
        assert result.status is CheckStatus.PASSED


# --------------------------------------------------------------------------- #
# Timeliness
# --------------------------------------------------------------------------- #


class TestFreshness:
    def test_stale_data_fails(self, ctx):
        result = run(
            ctx,
            type="freshness",
            dataset="main.orders",
            column="created_at",
            params={"max_age_hours": 1},
        )
        assert result.status is CheckStatus.FAILED
        assert result.observed["age_hours"] > 1

    def test_a_generous_window_passes(self, ctx):
        result = run(
            ctx,
            type="freshness",
            dataset="main.orders",
            column="created_at",
            params={"max_age_hours": 24 * 365 * 50},
        )
        assert result.status is CheckStatus.PASSED

    def test_an_empty_table_cannot_be_shown_fresh(self, ctx):
        result = run(
            ctx,
            type="freshness",
            dataset="main.empty_table",
            column="created_at",
            params={"max_age_hours": 1},
        )
        assert result.status is CheckStatus.FAILED
        assert "No timestamps" in result.message


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #


class TestReferentialIntegrity:
    def test_finds_the_orphan(self, ctx):
        result = run(
            ctx,
            type="referential_integrity",
            dataset="main.orders",
            column="customer_id",
            params={"to": "main.customers", "field": "id"},
        )
        assert result.status is CheckStatus.FAILED
        assert result.rows_failed == 1
        assert result.sample_rows[0]["orphan_value"] == "c99"

    def test_passes_when_every_value_resolves(self, ctx):
        result = run(
            ctx,
            type="referential_integrity",
            dataset="main.orders",
            column="customer_id",
            where="customer_id <> 'c99'",
            params={"to": "main.customers", "field": "id"},
        )
        assert result.status is CheckStatus.PASSED


class TestSchema:
    def test_passes_when_the_contract_holds(self, ctx):
        result = run(
            ctx,
            type="schema",
            dataset="main.clean_table",
            params={"columns": ["id", "label"]},
        )
        assert result.status is CheckStatus.PASSED

    def test_reports_a_missing_column(self, ctx):
        result = run(
            ctx,
            type="schema",
            dataset="main.clean_table",
            params={"columns": ["id", "label", "ghost"]},
        )
        assert result.status is CheckStatus.FAILED
        assert result.observed["missing"] == ["ghost"]

    def test_strict_mode_reports_extra_columns(self, ctx):
        result = run(
            ctx,
            type="schema",
            dataset="main.clean_table",
            params={"columns": ["id"], "strict": True},
        )
        assert result.status is CheckStatus.FAILED
        assert result.observed["unexpected"] == ["label"]

    def test_type_drift_is_detected(self, ctx):
        result = run(
            ctx,
            type="schema",
            dataset="main.clean_table",
            params={"columns": [{"name": "id", "type": "varchar"}]},
        )
        assert result.status is CheckStatus.FAILED
        assert result.observed["type_mismatches"]


class TestColumnExists:
    def test_missing_column(self, ctx):
        assert not run(ctx, type="column_exists", dataset="main.clean_table", column="ghost").passed

    def test_present_column(self, ctx):
        assert run(ctx, type="column_exists", dataset="main.clean_table", column="label").passed


# --------------------------------------------------------------------------- #
# Statistical
# --------------------------------------------------------------------------- #


class TestAggregate:
    def test_average_inside_bounds(self, ctx):
        result = run(
            ctx,
            type="aggregate",
            dataset="main.orders",
            column="total",
            params={"function": "avg", "min": 0, "max": 1000},
        )
        assert result.status is CheckStatus.PASSED

    def test_sum_outside_bounds(self, ctx):
        result = run(
            ctx,
            type="aggregate",
            dataset="main.orders",
            column="total",
            params={"function": "sum", "min": 1_000_000},
        )
        assert result.status is CheckStatus.FAILED

    def test_count_distinct(self, ctx):
        result = run(
            ctx,
            type="aggregate",
            dataset="main.customers",
            column="region",
            params={"function": "count_distinct", "equals": 4},
        )
        assert result.status is CheckStatus.PASSED

    def test_an_unknown_function_errors_clearly(self, ctx):
        result = run(
            ctx,
            type="aggregate",
            dataset="main.orders",
            column="total",
            params={"function": "median", "min": 0},
        )
        assert result.status is CheckStatus.ERRORED
        assert "Unsupported aggregate" in result.error


# --------------------------------------------------------------------------- #
# Custom SQL
# --------------------------------------------------------------------------- #


class TestCustomSQL:
    def test_scalar_expectation(self, ctx):
        result = run(
            ctx,
            type="custom_sql",
            query="SELECT COUNT(*) FROM main.orders WHERE total < 0",
            expect={"operator": "eq", "value": 0},
        )
        assert result.status is CheckStatus.FAILED
        assert result.observed == 1

    def test_ordering_expectation(self, ctx):
        result = run(
            ctx,
            type="custom_sql",
            query="SELECT COUNT(*) FROM main.orders",
            expect={"operator": "gte", "value": 5},
        )
        assert result.status is CheckStatus.PASSED

    def test_set_expectation_over_a_column(self, ctx):
        result = run(
            ctx,
            type="custom_sql",
            query="SELECT DISTINCT region FROM main.customers ORDER BY region",
            expect={
                "shape": "column",
                "operator": "set_equals",
                "value": ["apac", "emea", "mars", "namer"],
            },
        )
        assert result.status is CheckStatus.PASSED

    def test_table_template_is_substituted(self, ctx):
        result = run(
            ctx,
            type="custom_sql",
            dataset="main.clean_table",
            query="SELECT COUNT(*) FROM {{ table }}",
            expect={"operator": "eq", "value": 3},
        )
        assert result.status is CheckStatus.PASSED

    def test_no_expectation_defaults_to_zero_rows(self, ctx):
        result = run(ctx, type="custom_sql", query="SELECT * FROM main.orders WHERE total < -1000")
        assert result.status is CheckStatus.PASSED

    def test_a_write_statement_is_refused(self, ctx):
        result = run(ctx, type="custom_sql", query="DROP TABLE main.orders")
        assert result.status is CheckStatus.ERRORED
        assert "read-only" in result.error.lower() or "unsafe" in result.error.lower()

    def test_a_missing_query_errors_clearly(self, ctx):
        result = run(ctx, type="custom_sql")
        assert result.status is CheckStatus.ERRORED
        assert "requires a" in result.error


class TestSQLReturnsNoRows:
    def test_violating_rows_become_the_evidence(self, ctx):
        result = run(
            ctx,
            type="sql_returns_no_rows",
            query="SELECT order_id, total FROM main.orders WHERE total < 0",
        )
        assert result.status is CheckStatus.FAILED
        assert result.rows_failed == 1
        assert result.sample_rows[0]["order_id"] == "o6"

    def test_passes_when_nothing_violates(self, ctx):
        result = run(
            ctx,
            type="sql_returns_no_rows",
            query="SELECT * FROM main.orders WHERE total < -10000",
        )
        assert result.status is CheckStatus.PASSED


class TestSQLReturnsRows:
    def test_requires_at_least_one_row(self, ctx):
        assert run(ctx, type="sql_returns_rows", query="SELECT 1 FROM main.orders").passed
        assert not run(ctx, type="sql_returns_rows", query="SELECT 1 FROM main.empty_table").passed


class TestCompareQueries:
    def test_identical_results_pass(self, ctx):
        result = run(
            ctx,
            type="compare_queries",
            query="SELECT status, COUNT(*) FROM main.orders GROUP BY status",
            params={"other_query": "SELECT status, COUNT(*) FROM main.orders GROUP BY status"},
        )
        assert result.status is CheckStatus.PASSED

    def test_differing_results_fail(self, ctx):
        # orders has 7 rows, clean_table has 3.
        result = run(
            ctx,
            type="compare_queries",
            query="SELECT COUNT(*) FROM main.orders",
            params={"other_query": "SELECT COUNT(*) FROM main.clean_table"},
        )
        assert result.status is CheckStatus.FAILED
        assert result.observed == {"left_rows": 1, "right_rows": 1}


# --------------------------------------------------------------------------- #
# Cross-cutting behaviour
# --------------------------------------------------------------------------- #


class TestThresholds:
    def test_a_ratio_threshold_tolerates_a_small_failure(self, ctx):
        # 1 of 7 rows is null, which is ~14%.
        result = run(ctx, type="not_null", dataset="main.customers", column="email", threshold=0.2)
        assert result.status is CheckStatus.PASSED

    def test_a_ratio_threshold_still_fails_a_large_one(self, ctx):
        result = run(ctx, type="not_null", dataset="main.customers", column="email", threshold=0.05)
        assert result.status is CheckStatus.FAILED

    def test_an_absolute_threshold_tolerates_a_row_count(self, ctx):
        result = run(ctx, type="not_null", dataset="main.customers", column="email", threshold=5)
        assert result.status is CheckStatus.PASSED


class TestSeverityMapping:
    def test_warn_severity_produces_warned_not_failed(self, ctx):
        result = run(
            ctx,
            type="not_null",
            dataset="main.customers",
            column="email",
            severity=Severity.WARN,
        )
        assert result.status is CheckStatus.WARNED

    def test_error_severity_produces_failed(self, ctx):
        result = run(
            ctx,
            type="not_null",
            dataset="main.customers",
            column="email",
            severity=Severity.ERROR,
        )
        assert result.status is CheckStatus.FAILED


class TestErrorHandling:
    def test_a_missing_table_errors_rather_than_crashing(self, ctx):
        result = run(ctx, type="not_null", dataset="main.does_not_exist", column="x")
        assert result.status is CheckStatus.ERRORED
        assert result.error

    def test_a_missing_dataset_is_caught_before_any_sql(self, ctx):
        result = run(ctx, type="not_null", column="x")
        assert result.status is CheckStatus.ERRORED
        assert "requires a" in result.error

    def test_an_unknown_param_is_reported(self, ctx):
        result = run(ctx, type="row_count", dataset="main.orders", params={"minimum": 1})
        assert result.status is CheckStatus.ERRORED
        assert "unknown param" in result.error
