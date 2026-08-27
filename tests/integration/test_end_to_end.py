# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Whole-system behaviour: connect, catalog, run, record, report.

These are the tests that would catch a regression a user would actually notice.
"""

from __future__ import annotations

import json

import pytest

from nexassure.core.enums import CheckStatus, RunStatus
from nexassure.core.models import CheckSpec

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Connecting and cataloguing
# --------------------------------------------------------------------------- #


class TestConnect:
    def test_test_connection_reports_latency(self, na):
        outcome = na.test_connection("test_warehouse")
        assert outcome["ok"] is True
        assert outcome["latency_ms"] >= 0
        assert outcome["connector"] == "duckdb"

    def test_an_unknown_connection_fails_without_raising(self, na):
        outcome = na.test_connection("nope")
        assert outcome["ok"] is False
        assert "Unknown connection" in outcome["error"]

    def test_credentials_never_appear_in_the_summary(self, na):
        summaries = na.list_connections()
        assert summaries
        assert all(
            "password" not in json.dumps(s) or s["password"] in (None, "***") for s in summaries
        )


class TestAutomaticCataloguing:
    """The metadata tables must appear on their own, with no migration step."""

    def test_connecting_creates_the_metastore_tables(self, na):
        na.connect("test_warehouse")
        store = na.metastore
        assert store is not None

        from sqlalchemy import inspect

        tables = set(inspect(store.engine).get_table_names())
        assert {
            "nexassure_connections",
            "nexassure_datasets",
            "nexassure_columns",
            "nexassure_checks",
            "nexassure_runs",
            "nexassure_check_results",
            "nexassure_profiles",
            "nexassure_column_profiles",
            "nexassure_schedules",
            "nexassure_meta",
        } <= tables

    def test_connecting_registers_the_connection(self, na):
        na.connect("test_warehouse")
        connections = na.metastore.list_connections()
        assert [c["name"] for c in connections] == ["test_warehouse"]
        assert connections[0]["type"] == "duckdb"
        assert connections[0]["first_seen_at"] is not None

    def test_connecting_catalogs_tables_and_columns(self, na):
        na.connect("test_warehouse")
        datasets = na.metastore.list_datasets("test_warehouse")
        names = {d["table_name"] for d in datasets}
        assert {"customers", "orders", "clean_table", "empty_table"} <= names

        columns = na.metastore.get_dataset_columns("test_warehouse", "main.customers")
        assert {c["name"] for c in columns} == {
            "id",
            "email",
            "region",
            "signup_date",
            "lifetime_value",
        }

    def test_cataloguing_is_idempotent(self, na):
        first = na.discover("test_warehouse")
        second = na.discover("test_warehouse")
        assert first["datasets_registered"] == second["datasets_registered"]
        assert len(na.metastore.list_datasets("test_warehouse")) == first["datasets_registered"]

    def test_descriptions_can_be_attached_to_catalogued_objects(self, na):
        na.connect("test_warehouse")
        store = na.metastore
        assert store.set_dataset_description(
            "test_warehouse", "main.orders", "The orders fact table."
        )
        assert store.set_column_description(
            "test_warehouse", "main.orders", "total", "Gross order value in USD."
        )
        dataset = next(
            d for d in store.list_datasets("test_warehouse") if d["fqn"] == "main.orders"
        )
        assert dataset["description"] == "The orders fact table."


# --------------------------------------------------------------------------- #
# Running suites
# --------------------------------------------------------------------------- #


class TestRunSuite:
    def test_runs_every_check_and_reports_a_mixed_outcome(self, na):
        run = na.run_suite("orders_quality")
        assert run.summary.total == 5
        assert run.summary.passed == 3
        assert run.summary.failed == 2
        assert run.status is RunStatus.FAILED
        assert run.exit_code == 1

    def test_results_keep_declaration_order(self, na):
        run = na.run_suite("orders_quality")
        assert [r.check_name for r in run.results] == [
            "clean_ids_not_null",
            "clean_ids_unique",
            "orders_have_rows",
            "customer_ids_are_duplicated",
            "no_negative_totals",
        ]

    def test_failures_carry_the_business_description(self, na):
        run = na.run_suite("orders_quality")
        failure = next(r for r in run.failures() if r.check_name == "no_negative_totals")
        assert "negative total" in failure.description

    def test_select_narrows_the_run(self, na):
        run = na.run_suite("orders_quality", select=["orders_have_rows"])
        assert run.summary.total == 1
        assert run.status is RunStatus.PASSED

    def test_a_dry_run_touches_nothing(self, na):
        run = na.run_suite("orders_quality", dry_run=True)
        assert all(r.status is CheckStatus.SKIPPED for r in run.results)
        assert run.metadata["dry_run"] is True

    def test_fail_fast_stops_early(self, na):
        run = na.run_suite("orders_quality", fail_fast=True, max_parallel=1)
        assert run.summary.skipped > 0

    def test_a_progress_callback_sees_every_result(self, na):
        seen = []
        na.run_suite("orders_quality", on_result=seen.append)
        assert len(seen) == 5

    def test_an_unknown_suite_is_reported_clearly(self, na):
        from nexassure.exceptions import SuiteError

        with pytest.raises(SuiteError, match="Unknown suite"):
            na.run_suite("does_not_exist")

    def test_parallel_and_serial_runs_agree(self, na):
        parallel = na.run_suite("orders_quality", max_parallel=8, record=False)
        serial = na.run_suite("orders_quality", max_parallel=1, record=False)
        assert {(r.check_name, r.status) for r in parallel.results} == {
            (r.check_name, r.status) for r in serial.results
        }


class TestAdHocCheck:
    def test_runs_without_a_suite_file(self, na):
        result = na.run_check(
            "test_warehouse",
            CheckSpec(name="probe", type="not_null", dataset="main.customers", column="email"),
        )
        assert result.status is CheckStatus.FAILED
        assert result.rows_failed == 1


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


class TestHistory:
    def test_a_run_is_recorded_with_its_results(self, na):
        run = na.run_suite("orders_quality")
        stored = na.get_run(run.run_id)
        assert stored is not None
        assert stored["suite_name"] == "orders_quality"
        assert stored["failed"] == 2
        assert len(stored["results"]) == 5

    def test_sample_rows_survive_the_round_trip(self, na):
        run = na.run_suite("orders_quality")
        stored = na.get_run(run.run_id)
        duplicates = next(
            r for r in stored["results"] if r["check_name"] == "customer_ids_are_duplicated"
        )
        assert duplicates["sample_rows"]
        assert isinstance(duplicates["sample_rows"], list)

    def test_history_lists_runs_newest_first(self, na):
        first = na.run_suite("orders_quality")
        second = na.run_suite("orders_quality")
        ids = [r["run_id"] for r in na.history(limit=5)]
        assert ids[0] == second.run_id
        assert first.run_id in ids

    def test_check_history_tracks_one_check_over_time(self, na):
        na.run_suite("orders_quality")
        na.run_suite("orders_quality")
        check_id = next(
            c.check_id for c in na.suite("orders_quality").checks if c.name == "orders_have_rows"
        )
        assert len(na.metastore.check_history(check_id)) == 2

    def test_summary_aggregates_the_window(self, na):
        na.run_suite("orders_quality")
        summary = na.summary(24)
        assert summary["runs"] >= 1
        assert summary["checks_executed"] >= 5
        assert 0.0 <= summary["pass_rate"] <= 1.0
        assert summary["registered_datasets"] > 0

    def test_recent_failures_are_queryable(self, na):
        na.run_suite("orders_quality")
        failures = na.metastore.failing_checks(since_hours=24)
        assert {f["check_name"] for f in failures} >= {
            "customer_ids_are_duplicated",
            "no_negative_totals",
        }

    def test_syncing_registers_check_definitions(self, na):
        assert na.sync_suites() == 5
        registered = na.metastore.list_checks("orders_quality")
        assert len(registered) == 5
        assert all(c["description"] is not None or c["type"] for c in registered)

    def test_purge_removes_old_history_only(self, na):
        na.run_suite("orders_quality")
        assert na.metastore.purge(older_than_days=365) == {}
        assert len(na.history()) == 1


class TestProfileHistory:
    def test_profiles_are_recorded_and_retrievable(self, na):
        na.profile("test_warehouse", "main.customers")
        latest = na.metastore.latest_profile("main.customers")
        assert latest is not None
        assert latest["row_count"] == 7
        assert len(latest["columns"]) == 5

    def test_repeated_profiles_build_a_trend(self, na):
        na.profile("test_warehouse", "main.customers")
        na.profile("test_warehouse", "main.customers")
        assert len(na.metastore.profile_history("main.customers")) == 2
        assert len(na.metastore.profile_history("main.customers", column="email")) == 2


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


class TestMetastoreDegradation:
    def test_an_unreachable_metastore_does_not_fail_the_run(self, na, monkeypatch):
        """History is a nice-to-have; a metastore outage must not break testing."""
        from nexassure.metastore.repository import Metastore

        broken = Metastore("sqlite:////this/path/cannot/exist/metastore.db", strict=False)
        monkeypatch.setattr(type(na), "metastore", property(lambda self: broken))

        run = na.run_suite("orders_quality")
        assert run.summary.total == 5
        assert run.status is RunStatus.FAILED  # from the data, not from the metastore


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


class TestReporting:
    @pytest.fixture
    def run(self, na):
        return na.run_suite("orders_quality")

    def test_json_round_trips(self, run):
        from nexassure.reporting.exporters import to_json

        payload = json.loads(to_json(run))
        assert payload["suite_name"] == "orders_quality"
        assert len(payload["results"]) == 5

    def test_junit_is_well_formed_and_counts_correctly(self, run):
        from xml.etree import ElementTree as ET

        from nexassure.reporting.exporters import to_junit

        root = ET.fromstring(to_junit(run))
        assert root.tag == "testsuite"
        assert root.get("tests") == "5"
        assert root.get("failures") == "2"
        assert len(root.findall("testcase")) == 5
        assert len(root.findall("testcase/failure")) == 2

    def test_markdown_lists_the_failures(self, run):
        from nexassure.reporting.exporters import to_markdown

        rendered = to_markdown(run)
        assert "orders_quality" in rendered
        assert "no_negative_totals" in rendered

    def test_html_is_self_contained(self, run):
        from nexassure.reporting.exporters import to_html

        page = to_html(run)
        assert page.startswith("<!doctype html>")
        assert "<script src=" not in page
        assert "http://" not in page.replace("http://www.w3.org", "")
        assert "no_negative_totals" in page

    def test_write_report_infers_the_format(self, run, tmp_path):
        from nexassure.reporting.exporters import write_report

        for name, marker in (
            ("r.json", "{"),
            ("r.xml", "<?xml"),
            ("r.html", "<!doctype"),
            ("r.md", "##"),
        ):
            target = write_report(run, tmp_path / name)
            assert target.read_text(encoding="utf-8").lstrip().startswith(marker)

    def test_an_unknown_format_is_rejected(self, run, tmp_path):
        from nexassure.reporting.exporters import write_report

        with pytest.raises(ValueError, match="Unknown report format"):
            write_report(run, tmp_path / "r.txt", fmt="pdf")
