# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Dependency planning in the execution engine."""

from __future__ import annotations

import pytest

from nexassure.core.engine import plan_waves
from nexassure.core.models import CheckSpec
from nexassure.exceptions import SuiteError


def spec(name: str, depends_on: list[str] | None = None) -> CheckSpec:
    return CheckSpec(
        name=name, type="not_null", dataset="t", column="c", depends_on=depends_on or []
    )


class TestPlanWaves:
    def test_independent_checks_share_one_wave(self):
        waves = plan_waves([spec("a"), spec("b"), spec("c")])
        assert len(waves) == 1
        assert {s.name for s in waves[0]} == {"a", "b", "c"}

    def test_a_dependency_creates_a_second_wave(self):
        waves = plan_waves([spec("child", ["parent"]), spec("parent")])
        assert [s.name for s in waves[0]] == ["parent"]
        assert [s.name for s in waves[1]] == ["child"]

    def test_a_chain_creates_one_wave_per_link(self):
        waves = plan_waves([spec("c", ["b"]), spec("b", ["a"]), spec("a")])
        assert [[s.name for s in wave] for wave in waves] == [["a"], ["b"], ["c"]]

    def test_a_cycle_is_reported_rather_than_hanging(self):
        with pytest.raises(SuiteError, match="cycle"):
            plan_waves([spec("a", ["b"]), spec("b", ["a"])])

    def test_a_dependency_outside_the_selection_is_ignored(self):
        # Narrowing a run with --select must not deadlock on a dependency that
        # the selection filtered out.
        waves = plan_waves([spec("only", ["not_selected"])])
        assert [s.name for s in waves[0]] == ["only"]

    def test_every_check_is_scheduled_exactly_once(self):
        specs = [spec("a"), spec("b", ["a"]), spec("c", ["a"]), spec("d", ["b", "c"])]
        scheduled = [s.name for wave in plan_waves(specs) for s in wave]
        assert sorted(scheduled) == ["a", "b", "c", "d"]
        assert len(scheduled) == 4
