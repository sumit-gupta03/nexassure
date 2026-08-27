# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Profiling and profile-driven check suggestion, against a real database."""

from __future__ import annotations

import pytest

from nexassure.core.enums import ColumnKind
from nexassure.core.models import TableRef
from nexassure.profiling.inference import InferenceOptions, suggest_checks, suggest_suite
from nexassure.profiling.profiler import ProfileOptions, Profiler

pytestmark = pytest.mark.integration


@pytest.fixture
def customers_profile(connector):
    return Profiler(connector).profile_table(TableRef.parse("main.customers"))


class TestTableProfile:
    def test_counts_rows_and_columns(self, customers_profile):
        assert customers_profile.row_count == 7
        assert customers_profile.column_count == 5
        assert len(customers_profile.columns) == 5

    def test_records_the_source_and_timing(self, customers_profile):
        assert customers_profile.dataset.table == "customers"
        assert customers_profile.connection_name == "test_warehouse"
        assert customers_profile.duration_ms > 0

    def test_an_empty_table_profiles_without_error(self, connector):
        profile = Profiler(connector).profile_table(TableRef.parse("main.empty_table"))
        assert profile.row_count == 0
        assert all(c.null_count == 0 for c in profile.columns)


class TestColumnMetrics:
    def test_null_counts_and_ratios(self, customers_profile):
        email = customers_profile.column_profile("email")
        assert email.null_count == 1
        assert email.null_ratio == pytest.approx(1 / 7)
        assert email.completeness == pytest.approx(6 / 7)

    def test_blank_strings_are_counted_separately_from_nulls(self, customers_profile):
        assert customers_profile.column_profile("email").blank_count == 1

    def test_cardinality_and_duplicates(self, customers_profile):
        ids = customers_profile.column_profile("id")
        assert ids.distinct_count == 6
        assert ids.duplicate_count == 1
        assert ids.is_unique is False

    def test_a_genuinely_unique_column_is_flagged(self, connector):
        profile = Profiler(connector).profile_table(TableRef.parse("main.orders"))
        assert profile.column_profile("order_id").is_unique is True

    def test_numeric_statistics(self, customers_profile):
        ltv = customers_profile.column_profile("lifetime_value")
        assert ltv.kind is ColumnKind.NUMERIC
        assert float(ltv.min) == pytest.approx(150.75)
        assert float(ltv.max) == pytest.approx(2400.00)
        assert ltv.mean == pytest.approx(6042.25 / 7, rel=1e-6)
        assert ltv.sum == pytest.approx(6042.25)
        assert ltv.stddev is not None

    def test_string_lengths(self, customers_profile):
        region = customers_profile.column_profile("region")
        assert region.kind is ColumnKind.STRING
        assert region.min_length == 4
        assert region.max_length == 5

    def test_temporal_columns_get_bounds(self, customers_profile):
        signup = customers_profile.column_profile("signup_date")
        assert signup.kind is ColumnKind.TEMPORAL
        assert signup.min is not None and signup.max is not None

    def test_top_values_are_collected_for_low_cardinality_columns(self, customers_profile):
        region = customers_profile.column_profile("region")
        assert region.top_values
        assert region.top_values[0]["value"] == "emea"
        assert region.top_values[0]["count"] == 3
        assert region.top_values[0]["ratio"] == pytest.approx(3 / 7)

    def test_top_values_are_skipped_for_unique_columns(self, connector):
        # Ten arbitrary primary keys tell nobody anything.
        profile = Profiler(connector).profile_table(TableRef.parse("main.orders"))
        assert profile.column_profile("order_id").top_values == []


class TestProfileOptions:
    def test_duplicate_rows_are_counted_on_request(self, connector):
        profile = Profiler(connector, ProfileOptions(include_duplicate_rows=True)).profile_table(
            TableRef.parse("main.clean_table")
        )
        assert profile.duplicate_row_count == 0

    def test_percentiles_are_computed_on_request(self, connector):
        profile = Profiler(connector, ProfileOptions(include_percentiles=True)).profile_table(
            TableRef.parse("main.customers")
        )
        ltv = profile.column_profile("lifetime_value")
        assert ltv.median is not None
        assert ltv.p25 is not None and ltv.p95 is not None

    def test_where_narrows_the_profile(self, connector):
        profile = Profiler(connector, ProfileOptions(where="region = 'emea'")).profile_table(
            TableRef.parse("main.customers")
        )
        assert profile.row_count == 3

    def test_excluded_columns_are_not_profiled(self, connector):
        profile = Profiler(
            connector, ProfileOptions(exclude_columns=["email", "region"])
        ).profile_table(TableRef.parse("main.customers"))
        assert {c.column for c in profile.columns} == {"id", "signup_date", "lifetime_value"}

    def test_sampling_marks_the_profile(self, connector):
        profile = Profiler(connector, ProfileOptions(sample_rows=3)).profile_table(
            TableRef.parse("main.customers")
        )
        assert profile.sampled is True
        assert profile.row_count == 3

    def test_a_small_batch_size_still_profiles_every_column(self, connector):
        # Forces several batched queries rather than one.
        profile = Profiler(connector, ProfileOptions(batch_size=2)).profile_table(
            TableRef.parse("main.customers")
        )
        assert len(profile.columns) == 5
        assert profile.column_profile("email").null_count == 1


class TestSchemaProfiling:
    def test_profiles_every_table_in_a_schema(self, connector):
        profiles = Profiler(connector).profile_schema("main")
        assert {p.dataset.table for p in profiles} >= {
            "customers",
            "orders",
            "clean_table",
            "empty_table",
        }


class TestSuggestion:
    def test_suggests_not_null_only_for_populated_columns(self, customers_profile):
        checks = suggest_checks(customers_profile, InferenceOptions(min_rows_for_stats=1))
        by_type = {(c.type, c.column) for c in checks}
        assert ("not_null", "id") in by_type
        assert ("not_null", "email") not in by_type  # email has a NULL

    def test_does_not_suggest_unique_for_a_duplicated_column(self, customers_profile):
        checks = suggest_checks(customers_profile, InferenceOptions(min_rows_for_stats=1))
        assert ("unique", "id") not in {(c.type, c.column) for c in checks}

    def test_suggests_unique_for_a_real_key(self, connector):
        profile = Profiler(connector).profile_table(TableRef.parse("main.orders"))
        checks = suggest_checks(profile, InferenceOptions(min_rows_for_stats=1))
        assert ("unique", "order_id") in {(c.type, c.column) for c in checks}

    def test_suggests_accepted_values_for_a_low_cardinality_column(self, customers_profile):
        checks = suggest_checks(customers_profile, InferenceOptions(min_rows_for_stats=1))
        enum_checks = [c for c in checks if c.type == "accepted_values"]
        assert any(c.column == "region" for c in enum_checks)
        region = next(c for c in enum_checks if c.column == "region")
        assert set(region.params["values"]) == {"emea", "namer", "apac", "mars"}

    def test_suggests_a_row_count_floor(self, customers_profile):
        checks = suggest_checks(customers_profile, InferenceOptions(min_rows_for_stats=1))
        row_count = next(c for c in checks if c.type == "row_count")
        assert row_count.params["min"] == 3  # half of 7, floored

    def test_every_suggestion_carries_a_description_and_is_only_a_warning(self, customers_profile):
        checks = suggest_checks(customers_profile, InferenceOptions(min_rows_for_stats=1))
        assert checks
        for check in checks:
            assert check.description, check.name
            assert check.severity.value == "warn"
            assert "auto-suggested" in check.tags

    def test_statistics_are_withheld_below_the_row_threshold(self, customers_profile):
        # With a high min_rows_for_stats, only the evidence-free suggestions remain.
        checks = suggest_checks(customers_profile, InferenceOptions(min_rows_for_stats=1000))
        types = {c.type for c in checks}
        assert "accepted_values" not in types
        assert "unique" not in types

    def test_a_generated_suite_validates(self, customers_profile):
        from nexassure.suites.loader import validate_suite

        suite = suggest_suite(
            [customers_profile], "test_warehouse", options=InferenceOptions(min_rows_for_stats=1)
        )
        assert suite.checks
        assert validate_suite(suite) == []
