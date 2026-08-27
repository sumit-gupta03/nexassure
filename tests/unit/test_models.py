# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Domain model behaviour: parsing, defaults inheritance, status roll-up."""

from __future__ import annotations

import pytest

from nexassure.core.enums import CheckStatus, RunStatus, Severity
from nexassure.core.models import (
    CheckResult,
    CheckSpec,
    Expectation,
    RunResult,
    Suite,
    TableRef,
    resolve_env,
)


class TestTableRef:
    @pytest.mark.parametrize(
        "raw,database,schema,table",
        [
            ("orders", None, None, "orders"),
            ("public.orders", None, "public", "orders"),
            ("prod.public.orders", "prod", "public", "orders"),
            ("  public.orders  ", None, "public", "orders"),
        ],
    )
    def test_parses_each_qualification_depth(self, raw, database, schema, table):
        ref = TableRef.parse(raw)
        assert (ref.database, ref.db_schema, ref.table) == (database, schema, table)

    def test_quoted_identifier_may_contain_a_dot(self):
        ref = TableRef.parse('"odd.schema".orders')
        assert ref.db_schema == "odd.schema"
        assert ref.table == "orders"

    def test_more_than_three_parts_keeps_the_last_three(self):
        ref = TableRef.parse("server.prod.public.orders")
        assert (ref.database, ref.db_schema, ref.table) == ("prod", "public", "orders")

    def test_fqn_omits_missing_levels(self):
        assert TableRef.parse("public.orders").fqn == "public.orders"

    def test_empty_reference_is_rejected(self):
        with pytest.raises(ValueError):
            TableRef.parse("   ")


class TestEnvResolution:
    def test_expands_a_set_variable(self, monkeypatch):
        monkeypatch.setenv("ODQ_TEST_VALUE", "secret")
        assert resolve_env("pw=${env:ODQ_TEST_VALUE}") == "pw=secret"

    def test_uses_the_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("ODQ_MISSING", raising=False)
        assert resolve_env("${env:ODQ_MISSING:-fallback}") == "fallback"

    def test_unset_without_default_raises(self, monkeypatch):
        monkeypatch.delenv("ODQ_MISSING", raising=False)
        with pytest.raises(KeyError):
            resolve_env("${env:ODQ_MISSING}")


class TestCheckSpec:
    def test_column_and_columns_stay_in_sync(self):
        assert CheckSpec(name="a", type="not_null", column="id").columns == ["id"]
        assert CheckSpec(name="a", type="unique", columns=["a", "b"]).column == "a"

    def test_dataset_string_is_parsed(self):
        spec = CheckSpec(name="a", type="not_null", dataset="public.orders", column="id")
        assert spec.dataset.db_schema == "public"

    def test_check_id_is_stable_and_distinguishing(self):
        first = CheckSpec(name="a", type="not_null", dataset="t", column="id")
        same = CheckSpec(name="a", type="not_null", dataset="t", column="id")
        other = CheckSpec(name="a", type="not_null", dataset="t", column="other")
        assert first.check_id == same.check_id
        assert first.check_id != other.check_id


class TestSuiteDefaults:
    def test_defaults_fill_unset_fields_only(self):
        suite = Suite(
            name="s",
            connection="c",
            defaults={"severity": "warn", "schema": "analytics", "owner": "data-team"},
            checks=[
                {"name": "inherits", "type": "not_null", "dataset": "orders", "column": "id"},
                {
                    "name": "overrides",
                    "type": "not_null",
                    "dataset": "reporting.orders",
                    "column": "id",
                    "severity": "critical",
                },
            ],
        )
        inherits, overrides = suite.checks
        assert inherits.severity is Severity.WARN
        assert inherits.dataset.db_schema == "analytics"
        assert inherits.owner == "data-team"
        assert overrides.severity is Severity.CRITICAL
        assert overrides.dataset.db_schema == "reporting"

    def test_duplicate_check_names_are_rejected(self):
        with pytest.raises(ValueError, match="Duplicate check name"):
            Suite(
                name="s",
                connection="c",
                checks=[
                    {"name": "dup", "type": "not_null", "dataset": "t", "column": "a"},
                    {"name": "dup", "type": "not_null", "dataset": "t", "column": "b"},
                ],
            )

    def test_select_filters_by_name_tag_and_dataset(self):
        suite = Suite(
            name="s",
            connection="c",
            checks=[
                {"name": "a", "type": "not_null", "dataset": "t1", "column": "x", "tags": ["fast"]},
                {"name": "b", "type": "not_null", "dataset": "t2", "column": "y", "tags": ["slow"]},
                {"name": "c", "type": "not_null", "dataset": "t1", "column": "z", "enabled": False},
            ],
        )
        assert [c.name for c in suite.select()] == ["a", "b"]
        assert [c.name for c in suite.select(names=["a"])] == ["a"]
        assert [c.name for c in suite.select(tags=["slow"])] == ["b"]
        assert [c.name for c in suite.select(datasets=["t1"])] == ["a"]


class TestExpectationValidation:
    def test_operator_requiring_a_value_rejects_none(self):
        with pytest.raises(ValueError, match="requires a"):
            Expectation(operator="eq")

    def test_between_requires_two_bounds(self):
        with pytest.raises(ValueError, match="low, high"):
            Expectation(operator="between", value=[1])

    def test_presence_operators_need_no_value(self):
        assert Expectation(operator="not_empty").value is None


class TestRunResult:
    @staticmethod
    def _result(name: str, status: CheckStatus) -> CheckResult:
        return CheckResult(check_id=name, check_name=name, check_type="not_null", status=status)

    def test_recompute_counts_and_sets_status(self):
        run = RunResult(suite_name="s", connection_name="c")
        run.results = [
            self._result("a", CheckStatus.PASSED),
            self._result("b", CheckStatus.PASSED),
            self._result("c", CheckStatus.FAILED),
            self._result("d", CheckStatus.SKIPPED),
        ]
        run.recompute()
        assert (run.summary.passed, run.summary.failed, run.summary.skipped) == (2, 1, 1)
        assert run.status is RunStatus.FAILED
        assert run.exit_code == 1
        assert run.summary.pass_rate == pytest.approx(2 / 3)

    def test_errors_outrank_failures(self):
        run = RunResult(suite_name="s", connection_name="c")
        run.results = [
            self._result("a", CheckStatus.FAILED),
            self._result("b", CheckStatus.ERRORED),
        ]
        run.recompute()
        assert run.status is RunStatus.ERRORED

    def test_warnings_alone_do_not_fail_the_run(self):
        run = RunResult(suite_name="s", connection_name="c")
        run.results = [self._result("a", CheckStatus.WARNED)]
        run.recompute()
        assert run.status is RunStatus.PASSED
        assert run.exit_code == 0

    def test_empty_run_passes(self):
        run = RunResult(suite_name="s", connection_name="c").recompute()
        assert run.status is RunStatus.PASSED
        assert run.summary.pass_rate == 1.0


class TestSeverity:
    def test_only_error_and_critical_block(self):
        assert Severity.ERROR.blocking and Severity.CRITICAL.blocking
        assert not Severity.WARN.blocking and not Severity.INFO.blocking
