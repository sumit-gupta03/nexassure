# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Command line interface.

Exit codes are part of the published contract, so they are asserted explicitly:
0 = clean, 1 = checks failed, 2 = NexAssure could not run.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from nexassure.cli import EXIT_CHECKS_FAILED, EXIT_OK, EXIT_TOOL_ERROR, app

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    """Give Rich a wide terminal.

    CliRunner is not a TTY, so Rich falls back to 80 columns and truncates table
    cells - which would make assertions about rendered text fail for reasons
    that have nothing to do with the code under test.
    """
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setenv("TERM", "dumb")


def invoke(*args: str):
    return runner.invoke(app, list(args))


class TestMeta:
    def test_version(self):
        result = invoke("version")
        assert result.exit_code == EXIT_OK
        assert "nexassure" in result.stdout

    def test_help_lists_the_main_commands(self):
        result = invoke("--help")
        assert result.exit_code == EXIT_OK
        for command in ("run", "profile", "suggest", "validate", "mcp", "serve"):
            assert command in result.stdout

    def test_connectors_lists_every_supported_engine(self):
        result = invoke("connectors")
        assert result.exit_code == EXIT_OK
        for engine in ("snowflake", "postgres", "mssql", "redshift", "synapse", "oracle"):
            assert engine in result.stdout

    def test_connectors_as_json(self):
        result = invoke("connectors", "--json")
        assert result.exit_code == EXIT_OK
        rows = json.loads(result.stdout)
        assert {r["id"] for r in rows} >= {"snowflake", "postgres", "oracle"}

    def test_checks_lists_every_type(self):
        result = invoke("checks", "--json")
        assert result.exit_code == EXIT_OK
        types = {r["type"] for r in json.loads(result.stdout)}
        assert {"not_null", "unique", "custom_sql", "referential_integrity"} <= types


class TestInit:
    def test_scaffolds_a_working_project(self, tmp_path):
        result = invoke("init", str(tmp_path), "--name", "demo")
        assert result.exit_code == EXIT_OK
        assert (tmp_path / "nexassure.yml").is_file()
        assert (tmp_path / "suites" / "example.yml").is_file()
        assert "demo" in (tmp_path / "nexassure.yml").read_text(encoding="utf-8")

    def test_refuses_to_overwrite(self, tmp_path):
        invoke("init", str(tmp_path))
        result = invoke("init", str(tmp_path))
        assert result.exit_code == EXIT_TOOL_ERROR


class TestConnectionCommands:
    def test_test_connection(self, project_dir):
        result = invoke(
            "test-connection", "test_warehouse", "-c", str(project_dir / "nexassure.yml")
        )
        assert result.exit_code == EXIT_OK
        assert "OK" in result.stdout

    def test_test_connection_needs_a_target(self, project_dir):
        result = invoke("test-connection", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_TOOL_ERROR

    def test_unknown_connection_exits_two(self, project_dir):
        result = invoke("test-connection", "ghost", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_TOOL_ERROR

    def test_tables(self, project_dir):
        result = invoke("tables", "test_warehouse", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_OK
        assert "main.customers" in result.stdout

    def test_discover_populates_the_catalog(self, project_dir):
        result = invoke("discover", "test_warehouse", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_OK
        assert "Registered" in result.stdout

        listed = invoke("metastore", "catalog", "-c", str(project_dir / "nexassure.yml"))
        assert listed.exit_code == EXIT_OK
        assert "main.orders" in listed.stdout


class TestQuery:
    def test_read_query(self, project_dir):
        result = invoke(
            "query",
            "test_warehouse",
            "SELECT COUNT(*) AS n FROM main.orders",
            "-c",
            str(project_dir / "nexassure.yml"),
        )
        assert result.exit_code == EXIT_OK
        assert "7" in result.stdout

    def test_write_query_is_refused(self, project_dir):
        result = invoke(
            "query",
            "test_warehouse",
            "DROP TABLE main.orders",
            "-c",
            str(project_dir / "nexassure.yml"),
        )
        assert result.exit_code == EXIT_TOOL_ERROR


class TestProfile:
    def test_profiles_a_table(self, project_dir):
        result = invoke(
            "profile", "test_warehouse", "main.customers", "-c", str(project_dir / "nexassure.yml")
        )
        assert result.exit_code == EXIT_OK
        assert "customers" in result.stdout
        assert "lifetime_value" in result.stdout

    def test_writes_json_output(self, project_dir, tmp_path):
        target = tmp_path / "profile.json"
        result = invoke(
            "profile",
            "test_warehouse",
            "main.customers",
            "-o",
            str(target),
            "-c",
            str(project_dir / "nexassure.yml"),
        )
        assert result.exit_code == EXIT_OK
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["row_count"] == 7

    def test_requires_a_table_or_schema(self, project_dir):
        result = invoke("profile", "test_warehouse", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_TOOL_ERROR


class TestSuggest:
    def test_writes_a_valid_suite(self, project_dir, tmp_path):
        target = tmp_path / "generated.yml"
        result = invoke(
            "suggest",
            "test_warehouse",
            "-t",
            "main.customers",
            "-o",
            str(target),
            "-c",
            str(project_dir / "nexassure.yml"),
        )
        assert result.exit_code == EXIT_OK
        assert target.is_file()

        from nexassure.suites.loader import load_suite_file, validate_suite

        suite = load_suite_file(target)[0]
        assert suite.checks
        assert validate_suite(suite) == []


class TestValidate:
    def test_a_good_project_validates(self, project_dir):
        result = invoke("validate", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_OK
        assert "Valid" in result.stdout

    def test_a_broken_suite_exits_two(self, project_dir):
        (project_dir / "suites" / "broken.yml").write_text(
            "name: broken\nconnection: test_warehouse\n"
            "checks:\n  - {name: x, type: not_a_real_check, dataset: t}\n",
            encoding="utf-8",
        )
        result = invoke("validate", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_TOOL_ERROR
        assert "unknown check type" in result.stdout


class TestRun:
    def test_a_failing_suite_exits_one(self, project_dir):
        result = invoke("run", "orders_quality", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_CHECKS_FAILED
        assert "FAILED" in result.stdout

    def test_a_passing_selection_exits_zero(self, project_dir):
        result = invoke(
            "run",
            "orders_quality",
            "--select",
            "orders_have_rows",
            "-c",
            str(project_dir / "nexassure.yml"),
        )
        assert result.exit_code == EXIT_OK

    def test_dry_run_does_not_fail(self, project_dir):
        result = invoke(
            "run", "orders_quality", "--dry-run", "-c", str(project_dir / "nexassure.yml")
        )
        assert result.exit_code == EXIT_OK

    def test_unknown_suite_exits_two(self, project_dir):
        result = invoke("run", "ghost", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_TOOL_ERROR

    @pytest.mark.parametrize(
        "filename,marker",
        [("r.json", "{"), ("r.xml", "<?xml"), ("r.html", "<!doctype"), ("r.md", "##")],
    )
    def test_report_formats(self, project_dir, tmp_path, filename, marker):
        target = tmp_path / filename
        result = invoke(
            "run", "orders_quality", "-o", str(target), "-c", str(project_dir / "nexassure.yml")
        )
        assert result.exit_code == EXIT_CHECKS_FAILED
        assert target.read_text(encoding="utf-8").lstrip().startswith(marker)

    def test_an_unknown_format_exits_two(self, project_dir, tmp_path):
        result = invoke(
            "run",
            "orders_quality",
            "-o",
            str(tmp_path / "r.out"),
            "-f",
            "pdf",
            "-c",
            str(project_dir / "nexassure.yml"),
        )
        assert result.exit_code == EXIT_TOOL_ERROR


class TestHistoryAndMetastore:
    def test_history_after_a_run(self, project_dir):
        invoke("run", "orders_quality", "-c", str(project_dir / "nexassure.yml"))
        result = invoke("history", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_OK
        assert "orders_quality" in result.stdout

    def test_metastore_info(self, project_dir):
        result = invoke("metastore", "info", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_OK
        assert "Metastore" in result.stdout

    def test_metastore_sync(self, project_dir):
        result = invoke("metastore", "sync", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_OK
        assert "Synced" in result.stdout

    def test_purge_requires_confirmation(self, project_dir):
        result = invoke("metastore", "purge", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code != EXIT_OK  # aborted at the prompt


class TestSchedule:
    def test_list_with_no_schedules(self, project_dir):
        result = invoke("schedule", "list", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_OK
        assert "No suite" in result.stdout

    def test_list_shows_a_scheduled_suite(self, project_dir):
        path = project_dir / "suites" / "orders.yml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "connection: test_warehouse", 'connection: test_warehouse\nschedule: "0 6 * * *"'
            ),
            encoding="utf-8",
        )
        result = invoke("schedule", "list", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_OK
        assert "0 6 * * *" in result.stdout

    def test_scheduler_refuses_to_start_with_nothing_to_run(self, project_dir):
        result = invoke("schedule", "run", "-c", str(project_dir / "nexassure.yml"))
        assert result.exit_code == EXIT_TOOL_ERROR
