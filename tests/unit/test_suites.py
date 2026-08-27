# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Suite loading, validation and round-tripping."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexassure.core.models import Suite
from nexassure.exceptions import SuiteError
from nexassure.suites.loader import dump_suite, load_suite_file, load_suites, validate_suite

SINGLE = """
name: one
connection: c
checks:
  - name: a
    type: not_null
    dataset: t
    column: x
"""

MULTI = """
suites:
  - name: first
    connection: c
    checks:
      - {name: a, type: not_null, dataset: t, column: x}
  - name: second
    connection: c
    checks:
      - {name: b, type: not_null, dataset: t, column: y}
"""

BARE_LIST = """
- name: listed
  connection: c
  checks:
    - {name: a, type: not_null, dataset: t, column: x}
"""


def write(tmp_path: Path, body: str, name: str = "suite.yml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestLoading:
    def test_single_suite_mapping(self, tmp_path):
        suites = load_suite_file(write(tmp_path, SINGLE))
        assert [s.name for s in suites] == ["one"]
        assert suites[0].source_path

    def test_suites_key(self, tmp_path):
        assert [s.name for s in load_suite_file(write(tmp_path, MULTI))] == ["first", "second"]

    def test_bare_list(self, tmp_path):
        assert [s.name for s in load_suite_file(write(tmp_path, BARE_LIST))] == ["listed"]

    def test_empty_file_yields_nothing(self, tmp_path):
        assert load_suite_file(write(tmp_path, "")) == []

    def test_missing_file_is_reported(self, tmp_path):
        with pytest.raises(SuiteError, match="not found"):
            load_suite_file(tmp_path / "nope.yml")

    def test_malformed_yaml_is_reported(self, tmp_path):
        with pytest.raises(SuiteError, match="parse"):
            load_suite_file(write(tmp_path, "name: [unclosed"))

    def test_scalar_document_is_rejected(self, tmp_path):
        with pytest.raises(SuiteError, match="mapping"):
            load_suite_file(write(tmp_path, "just a string"))

    def test_duplicate_suite_names_across_files_are_rejected(self, tmp_path):
        first = write(tmp_path, SINGLE, "a.yml")
        second = write(tmp_path, SINGLE, "b.yml")
        with pytest.raises(SuiteError, match="Duplicate suite name"):
            load_suites([first, second])


class TestValidation:
    def test_a_good_suite_has_no_problems(self):
        suite = Suite(
            name="s",
            connection="c",
            checks=[{"name": "a", "type": "not_null", "dataset": "t", "column": "x"}],
        )
        assert validate_suite(suite) == []

    def test_unknown_check_type(self):
        suite = Suite(
            name="s", connection="c", checks=[{"name": "a", "type": "nope", "dataset": "t"}]
        )
        assert any("unknown check type" in p for p in validate_suite(suite))

    def test_missing_required_column(self):
        suite = Suite(
            name="s", connection="c", checks=[{"name": "a", "type": "not_null", "dataset": "t"}]
        )
        assert any("requires a" in p and "column" in p for p in validate_suite(suite))

    def test_custom_sql_without_a_query(self):
        suite = Suite(name="s", connection="c", checks=[{"name": "a", "type": "custom_sql"}])
        assert any("requires a" in p and "query" in p for p in validate_suite(suite))

    def test_unsupported_param_is_caught(self):
        suite = Suite(
            name="s",
            connection="c",
            checks=[{"name": "a", "type": "row_count", "dataset": "t", "params": {"minimum": 5}}],
        )
        assert any("unsupported param" in p for p in validate_suite(suite))

    def test_unknown_dependency(self):
        suite = Suite(
            name="s",
            connection="c",
            checks=[
                {
                    "name": "a",
                    "type": "not_null",
                    "dataset": "t",
                    "column": "x",
                    "depends_on": ["ghost"],
                }
            ],
        )
        assert any("unknown check" in p and "ghost" in p for p in validate_suite(suite))

    def test_dependency_cycle(self):
        suite = Suite(
            name="s",
            connection="c",
            checks=[
                {
                    "name": "a",
                    "type": "not_null",
                    "dataset": "t",
                    "column": "x",
                    "depends_on": ["b"],
                },
                {
                    "name": "b",
                    "type": "not_null",
                    "dataset": "t",
                    "column": "y",
                    "depends_on": ["a"],
                },
            ],
        )
        assert any("cycle" in p for p in validate_suite(suite))

    def test_an_empty_suite_is_flagged(self):
        assert any("no checks" in p for p in validate_suite(Suite(name="s", connection="c")))


class TestDump:
    def test_a_dumped_suite_reloads_identically(self, tmp_path):
        suite = Suite(
            name="round_trip",
            connection="c",
            description="Survives a save and reload.",
            checks=[
                {
                    "name": "a",
                    "type": "accepted_values",
                    "dataset": "analytics.orders",
                    "column": "status",
                    "description": "Only these states render downstream.",
                    "params": {"values": ["x", "y"]},
                    "severity": "warn",
                }
            ],
        )
        target = tmp_path / "out.yml"
        dump_suite(suite, target)

        reloaded = load_suite_file(target)[0]
        assert reloaded.name == "round_trip"
        assert reloaded.connection == "c"
        assert len(reloaded.checks) == 1

        check = reloaded.checks[0]
        assert check.type == "accepted_values"
        assert check.dataset.fqn == "analytics.orders"
        assert check.params == {"values": ["x", "y"]}
        assert check.severity.value == "warn"
        assert validate_suite(reloaded) == []
