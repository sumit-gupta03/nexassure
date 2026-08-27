# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""The suite execution engine.

Checks in a suite are independent by default, so they all run concurrently
against one connection pool - which is the difference between a 200-check suite
taking four minutes and taking four seconds on a warehouse that parallelises
well.

Concurrency is bounded three ways:

* ``max_parallel`` on the suite caps in-flight checks,
* the connector pool caps actual database sessions,
* ``depends_on`` splits execution into sequential waves.

Dependencies exist for the case where an expensive check is pointless if a
cheap one already failed - there is no reason to scan a fact table for orphans
when the table turned out to be empty. A check whose dependency failed is
reported ``SKIPPED``, never silently dropped.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor

from ..checks.base import CheckContext, build_check
from ..connectors.base import BaseConnector
from ..exceptions import SuiteError
from ..logging_conf import get_logger
from .enums import CheckStatus, RunStatus
from .models import CheckResult, CheckSpec, RunResult, Suite, utcnow

log = get_logger(__name__)

#: Called with each result as it lands, for live progress reporting.
ProgressCallback = Callable[[CheckResult], None]


class SuiteRunner:
    """Executes the checks of one suite against one connection."""

    def __init__(
        self,
        connector: BaseConnector,
        *,
        max_parallel: int | None = None,
        fail_fast: bool | None = None,
        environment: str | None = None,
        on_result: ProgressCallback | None = None,
    ) -> None:
        self.connector = connector
        self.max_parallel_override = max_parallel
        self.fail_fast_override = fail_fast
        self.environment = environment
        self.on_result = on_result

    # -- public API --------------------------------------------------------- #

    def run(
        self,
        suite: Suite,
        *,
        select: list[str] | None = None,
        tags: list[str] | None = None,
        datasets: list[str] | None = None,
        triggered_by: str = "manual",
        dry_run: bool = False,
    ) -> RunResult:
        """Run a suite and return its result.

        Args:
            suite: The suite to execute.
            select: Only run checks with these names.
            tags: Only run checks carrying at least one of these tags.
            datasets: Only run checks targeting these fully-qualified tables.
            triggered_by: Recorded on the run - ``manual``, ``schedule``, ``api``, ``mcp``.
            dry_run: Resolve and report the plan without touching the warehouse.
        """
        started = time.perf_counter()
        run = RunResult(
            suite_name=suite.name,
            connection_name=suite.connection,
            status=RunStatus.RUNNING,
            started_at=utcnow(),
            triggered_by=triggered_by,
            environment=self.environment,
        )

        chosen = suite.select(names=select, tags=tags, datasets=datasets)
        run.metadata = {
            "checks_declared": len(suite.checks),
            "checks_selected": len(chosen),
            "connector": self.connector.name,
            "dry_run": dry_run,
        }

        if not chosen:
            run.status = RunStatus.PASSED
            run.finished_at = utcnow()
            run.error = "No checks matched the selection"
            log.warning("Suite %r ran with no matching checks", suite.name)
            return run.recompute()

        if dry_run:
            run.results = [self._planned(spec) for spec in chosen]
            run.finished_at = utcnow()
            run.duration_ms = round((time.perf_counter() - started) * 1000, 3)
            run.status = RunStatus.PASSED
            return run.recompute()

        max_parallel = self.max_parallel_override or suite.max_parallel
        fail_fast = suite.fail_fast if self.fail_fast_override is None else self.fail_fast_override
        ctx = CheckContext(connector=self.connector, suite_name=suite.name, run_id=run.run_id)

        waves = plan_waves(chosen)
        log.info(
            "Running suite %r: %s check(s) in %s wave(s), up to %s in parallel",
            suite.name,
            len(chosen),
            len(waves),
            max_parallel,
        )

        completed: dict[str, CheckResult] = {}
        aborted = False

        for wave_index, wave in enumerate(waves):
            if aborted:
                for spec in wave:
                    completed[spec.name] = self._skipped(spec, "Run stopped by fail_fast")
                continue

            runnable, skipped = self._partition(wave, completed)
            for spec, reason in skipped:
                result = self._skipped(spec, reason)
                completed[spec.name] = result
                self._emit(result)

            if not runnable:
                continue

            log.debug("Wave %s: executing %s check(s)", wave_index + 1, len(runnable))
            for result in self._execute(runnable, ctx, max_parallel, fail_fast):
                completed[result.check_name] = result
                self._emit(result)
                if fail_fast and result.status in (CheckStatus.FAILED, CheckStatus.ERRORED):
                    log.warning("fail_fast: stopping after %r", result.check_name)
                    aborted = True

            if aborted:
                # Checks cancelled inside the wave still need a result row, so
                # the report accounts for every selected check.
                for spec in runnable:
                    if spec.name not in completed:
                        result = self._skipped(spec, "Run stopped by fail_fast")
                        completed[spec.name] = result
                        self._emit(result)

        # Preserve declaration order in the report, whatever order they finished in.
        run.results = [completed[spec.name] for spec in chosen if spec.name in completed]
        run.finished_at = utcnow()
        run.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        run.recompute()

        log.info(
            "Suite %r finished %s in %.0fms (%s passed, %s failed, %s errored, %s skipped)",
            suite.name,
            run.status.value,
            run.duration_ms,
            run.summary.passed,
            run.summary.failed,
            run.summary.errored,
            run.summary.skipped,
        )
        return run

    # -- internals ---------------------------------------------------------- #

    def _execute(
        self,
        specs: list[CheckSpec],
        ctx: CheckContext,
        max_parallel: int,
        stop_on_failure: bool = False,
    ) -> Iterable[CheckResult]:
        """Run one wave, yielding results as they complete.

        With ``stop_on_failure``, the first failing check cancels every queued
        check that has not started yet. Checks already running are allowed to
        finish - there is no safe way to interrupt a query mid-flight - but
        their results are not reported, and the caller marks them skipped.
        """
        if len(specs) == 1:
            yield build_check(specs[0]).run(ctx)
            return

        workers = max(1, min(max_parallel, len(specs)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nexassure-check") as pool:
            futures: dict[Future[CheckResult], CheckSpec] = {
                pool.submit(build_check(spec).run, ctx): spec for spec in specs
            }
            stopped = False
            for future in _as_completed(futures):
                if stopped:
                    continue
                spec = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    # Check.run already traps evaluation errors, so reaching here
                    # means the worker itself died - still worth a result row.
                    log.error("Check %r crashed: %s", spec.name, exc, exc_info=True)
                    result = self._errored(spec, str(exc))

                yield result

                if stop_on_failure and result.status in (
                    CheckStatus.FAILED,
                    CheckStatus.ERRORED,
                ):
                    stopped = True
                    for pending in futures:
                        pending.cancel()

    @staticmethod
    def _partition(
        wave: list[CheckSpec], completed: dict[str, CheckResult]
    ) -> tuple[list[CheckSpec], list[tuple[CheckSpec, str]]]:
        """Split a wave into checks that can run and checks blocked by a dependency."""
        runnable: list[CheckSpec] = []
        skipped: list[tuple[CheckSpec, str]] = []
        for spec in wave:
            blockers = [
                dependency
                for dependency in spec.depends_on
                if dependency in completed and not completed[dependency].passed
            ]
            if blockers:
                skipped.append((spec, f"Dependency failed: {', '.join(blockers)}"))
            else:
                runnable.append(spec)
        return runnable, skipped

    def _emit(self, result: CheckResult) -> None:
        if self.on_result is None:
            return
        try:
            self.on_result(result)
        except Exception as exc:  # pragma: no cover - a bad callback must not fail a run
            log.debug("Progress callback raised: %s", exc)

    @staticmethod
    def _base_result(spec: CheckSpec, status: CheckStatus, message: str) -> CheckResult:
        return CheckResult(
            check_id=spec.check_id,
            check_name=spec.name,
            check_type=spec.type,
            status=status,
            severity=spec.severity,
            description=spec.description,
            dataset=spec.dataset.fqn if spec.dataset else None,
            column=spec.column,
            message=message,
            tags=list(spec.tags),
            owner=spec.owner,
        )

    def _skipped(self, spec: CheckSpec, reason: str) -> CheckResult:
        return self._base_result(spec, CheckStatus.SKIPPED, reason)

    def _errored(self, spec: CheckSpec, error: str) -> CheckResult:
        result = self._base_result(spec, CheckStatus.ERRORED, f"Check crashed: {error}")
        result.error = error
        return result

    def _planned(self, spec: CheckSpec) -> CheckResult:
        return self._base_result(
            spec, CheckStatus.SKIPPED, f"Dry run: would execute {spec.type} check"
        )


def plan_waves(specs: list[CheckSpec]) -> list[list[CheckSpec]]:
    """Group checks into dependency waves.

    Wave 0 holds everything with no unresolved dependencies; wave *n* holds
    checks whose dependencies all completed by wave *n-1*. Within a wave,
    execution order is arbitrary and fully parallel.

    Raises:
        SuiteError: if the dependency graph contains a cycle.
    """
    remaining = {spec.name: spec for spec in specs}
    selected = set(remaining)
    waves: list[list[CheckSpec]] = []
    satisfied: set[str] = set()

    while remaining:
        wave = [
            spec
            for spec in remaining.values()
            # Dependencies outside the current selection cannot be waited on,
            # so they are treated as already satisfied rather than deadlocking.
            if all(d in satisfied or d not in selected for d in spec.depends_on)
        ]
        if not wave:
            stuck = ", ".join(sorted(remaining))
            raise SuiteError(
                f"Dependency cycle or unresolvable dependency among: {stuck}",
                checks=sorted(remaining),
            )
        waves.append(wave)
        for spec in wave:
            satisfied.add(spec.name)
            del remaining[spec.name]

    return waves


def _as_completed(futures: dict[Future[CheckResult], CheckSpec]):
    """``concurrent.futures.as_completed``, imported lazily to keep the module light."""
    from concurrent.futures import as_completed

    return as_completed(futures)
