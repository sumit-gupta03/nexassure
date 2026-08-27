# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Check base classes and the check-type registry.

A *check* turns a :class:`~nexassure.core.models.CheckSpec` into a
:class:`~nexassure.core.models.CheckResult`. Subclasses implement
:meth:`Check.evaluate` and return a small :class:`Outcome`; the base class owns
everything that should behave identically for every check type - timing, error
capture, threshold arithmetic, severity-to-status mapping and sample capture.

Most row-level checks are expressible as "which rows are bad?", so
:class:`RowPredicateCheck` implements that whole pattern once. Subclasses of it
only supply a SQL predicate.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..connectors.base import BaseConnector, Dialect
from ..core.enums import CheckStatus, Severity
from ..core.models import CheckResult, CheckSpec, utcnow
from ..exceptions import CheckExecutionError, UnknownCheckType
from ..logging_conf import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Outcome:
    """What a check observed, before threshold and severity are applied."""

    passed: bool
    observed: Any = None
    expected: Any = None
    message: str = ""
    rows_scanned: int | None = None
    rows_failed: int | None = None
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    query: str | None = None


@dataclass(slots=True)
class CheckContext:
    """Everything a check needs to talk to the warehouse."""

    connector: BaseConnector
    suite_name: str = ""
    run_id: str = ""

    @property
    def dialect(self) -> Dialect:
        return self.connector.dialect


class Check(ABC):
    """Base class for every check type."""

    #: Value users write as ``type:`` in a suite file.
    type_name: ClassVar[str] = ""
    #: One-line summary, surfaced by ``nexassure checks`` and the MCP tool listing.
    summary: ClassVar[str] = ""
    #: Whether ``dataset`` must be present on the spec.
    requires_dataset: ClassVar[bool] = True
    #: Whether at least one column must be named.
    requires_column: ClassVar[bool] = False
    #: Documented parameter names, for validation and generated docs.
    supported_params: ClassVar[tuple[str, ...]] = ()

    def __init__(self, spec: CheckSpec) -> None:
        self.spec = spec

    # -- subclass hook ------------------------------------------------------ #

    @abstractmethod
    def evaluate(self, ctx: CheckContext) -> Outcome:
        """Run the check and report what was observed."""

    # -- shared plumbing ---------------------------------------------------- #

    def validate(self) -> None:
        """Catch mis-declared checks before touching the warehouse."""
        if self.requires_dataset and self.spec.dataset is None:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} of type {self.type_name!r} requires a 'dataset'",
                check=self.spec.name,
            )
        if self.requires_column and not self.spec.columns:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} of type {self.type_name!r} requires a 'column'",
                check=self.spec.name,
            )
        unknown = set(self.spec.params) - set(self.supported_params)
        if unknown and self.supported_params:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} got unknown param(s): {', '.join(sorted(unknown))}. "
                f"Supported: {', '.join(self.supported_params) or 'none'}",
                check=self.spec.name,
            )

    def run(self, ctx: CheckContext) -> CheckResult:
        """Execute the check and build a fully-populated result.

        Never raises: an exception becomes an ``ERRORED`` result, so one broken
        check cannot abort a whole suite.
        """
        started = time.perf_counter()
        started_at = utcnow()
        spec = self.spec

        result = CheckResult(
            check_id=spec.check_id,
            check_name=spec.name,
            check_type=spec.type,
            status=CheckStatus.ERRORED,
            severity=spec.severity,
            description=spec.description,
            dataset=spec.dataset.fqn if spec.dataset else None,
            column=spec.column,
            started_at=started_at,
            tags=list(spec.tags),
            owner=spec.owner,
        )

        try:
            self.validate()
            outcome = self.evaluate(ctx)
            result.observed = outcome.observed
            result.expected = outcome.expected
            result.rows_scanned = outcome.rows_scanned
            result.rows_failed = outcome.rows_failed
            result.sample_rows = outcome.sample_rows[: spec.sample_limit]
            result.query = outcome.query
            result.failed_ratio = self._ratio(outcome)
            result.status = self._status(outcome, result.failed_ratio)
            result.message = outcome.message or self._default_message(result, outcome)
        except Exception as exc:
            result.status = CheckStatus.ERRORED
            result.error = str(exc)
            result.message = f"Check errored: {exc}"
            log.debug("Check %s errored", spec.name, exc_info=True)

        result.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return result

    @staticmethod
    def _ratio(outcome: Outcome) -> float | None:
        if outcome.rows_failed is None or not outcome.rows_scanned:
            return None
        return outcome.rows_failed / outcome.rows_scanned

    def _status(self, outcome: Outcome, failed_ratio: float | None) -> CheckStatus:
        """Map an outcome to a status, honouring ``threshold`` and ``severity``.

        ``threshold`` is read as a ratio when it is at most 1 and as an absolute
        row count above that, which is how people naturally write it: ``0.01``
        means "up to 1% may fail", ``50`` means "up to 50 rows may fail".
        """
        passed = outcome.passed
        threshold = self.spec.threshold

        if not passed and threshold is not None and outcome.rows_failed is not None:
            if threshold <= 1.0:
                if failed_ratio is not None and failed_ratio <= threshold:
                    passed = True
            elif outcome.rows_failed <= threshold:
                passed = True

        if passed:
            return CheckStatus.PASSED
        return CheckStatus.WARNED if self.spec.severity is Severity.WARN else CheckStatus.FAILED

    def _default_message(self, result: CheckResult, outcome: Outcome) -> str:
        target = result.dataset or "query"
        if result.column:
            target = f"{target}.{result.column}"
        if result.status is CheckStatus.PASSED:
            return f"{self.type_name} passed on {target}"
        if outcome.rows_failed is not None and outcome.rows_scanned:
            pct = (outcome.rows_failed / outcome.rows_scanned) * 100
            return (
                f"{self.type_name} failed on {target}: "
                f"{outcome.rows_failed:,} of {outcome.rows_scanned:,} rows ({pct:.2f}%)"
            )
        return f"{self.type_name} failed on {target}: observed {outcome.observed!r}"

    # -- helpers for subclasses --------------------------------------------- #

    def qualified_table(self, ctx: CheckContext) -> str:
        assert self.spec.dataset is not None
        return ctx.dialect.qualify(self.spec.dataset)

    def col(self, ctx: CheckContext, name: str | None = None) -> str:
        """Quote a column name from the spec."""
        column = name or self.spec.column
        if not column:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} needs a column", check=self.spec.name
            )
        return ctx.dialect.quote(column)

    def where_clause(self, extra: str | None = None) -> str:
        """Combine the spec-level ``where`` filter with a check-specific predicate."""
        parts = [p for p in (self.spec.where, extra) if p]
        return f" WHERE {' AND '.join(f'({p})' for p in parts)}" if parts else ""

    def param(self, name: str, default: Any = None) -> Any:
        return self.spec.params.get(name, default)

    def required_param(self, name: str) -> Any:
        if name not in self.spec.params:
            raise CheckExecutionError(
                f"Check {self.spec.name!r} of type {self.type_name!r} requires param {name!r}",
                check=self.spec.name,
                param=name,
            )
        return self.spec.params[name]


class RowPredicateCheck(Check):
    """Base for checks shaped as "which rows violate this rule?".

    A subclass returns a SQL predicate that is **true for failing rows**. The
    base then issues a single aggregate query that counts scanned and failing
    rows together, and a second query only when there is something to sample.
    Two round trips per check keeps warehouse cost predictable.
    """

    #: Included in the failure message, e.g. "3 null values".
    violation_noun: ClassVar[str] = "violating rows"

    @abstractmethod
    def failing_predicate(self, ctx: CheckContext) -> str:
        """SQL boolean expression that is TRUE for rows that fail the check."""

    def evaluate(self, ctx: CheckContext) -> Outcome:
        table = self.qualified_table(ctx)
        predicate = self.failing_predicate(ctx)
        scope = self.where_clause()

        # CASE WHEN ... THEN 1 ELSE 0 works everywhere; FILTER and COUNT_IF do not.
        count_sql = (
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) AS failing "
            f"FROM {table}{scope}"
        )
        row = ctx.connector.execute(count_sql, max_rows=1).first()
        total = int(row[0] or 0) if row else 0
        failing = int(row[1] or 0) if row and row[1] is not None else 0

        samples: list[dict[str, Any]] = []
        if failing and self.spec.sample_limit:
            samples = self._sample(ctx, table, predicate)

        return Outcome(
            passed=failing == 0,
            observed=failing,
            expected=0,
            rows_scanned=total,
            rows_failed=failing,
            sample_rows=samples,
            query=count_sql,
            message=(
                f"Found {failing:,} {self.violation_noun} in {total:,} rows"
                if failing
                else f"No {self.violation_noun} in {total:,} rows"
            ),
        )

    def _sample(self, ctx: CheckContext, table: str, predicate: str) -> list[dict[str, Any]]:
        """Fetch a handful of offending rows so failures are actionable, not just counted."""
        sample_sql = ctx.dialect.limit(
            f"SELECT * FROM {table}{self.where_clause(predicate)}", self.spec.sample_limit
        )
        try:
            return ctx.connector.execute(sample_sql, max_rows=self.spec.sample_limit).dicts()
        except Exception as exc:
            log.debug("Could not sample failing rows for %s: %s", self.spec.name, exc)
            return []


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, type[Check]] = {}
_ALIASES: dict[str, str] = {}
_PLUGINS_LOADED = False


def register_check(cls: type[Check], *aliases: str) -> type[Check]:
    """Register a check class under its ``type_name`` plus any aliases.

    Usable as a decorator::

        @register_check
        class MyCheck(Check):
            type_name = "my_check"
    """
    if not cls.type_name:
        raise ValueError(f"{cls.__name__} must define type_name")
    _REGISTRY[cls.type_name] = cls
    for alias in aliases:
        _ALIASES[alias] = cls.type_name
    return cls


def _load_plugins() -> None:
    """Pull in built-ins and any third-party ``nexassure.checks`` entry points."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    _PLUGINS_LOADED = True

    from . import builtin, custom_sql  # noqa: F401  (import registers the classes)

    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="nexassure.checks"):
            if ep.value.startswith("nexassure.checks."):
                continue  # already imported above
            try:
                loaded = ep.load()
                if callable(loaded):
                    loaded()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Could not load check plugin %s: %s", ep.name, exc)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("Could not scan check entry points: %s", exc)


def get_check_class(type_name: str) -> type[Check]:
    """Resolve a ``type:`` string to its check class."""
    _load_plugins()
    key = (type_name or "").strip().lower()
    key = _ALIASES.get(key, key)
    if key not in _REGISTRY:
        raise UnknownCheckType(
            f"Unknown check type {type_name!r}. Available: {', '.join(available_checks())}",
            requested=type_name,
            available=available_checks(),
        )
    return _REGISTRY[key]


def build_check(spec: CheckSpec) -> Check:
    """Instantiate the right check class for a spec."""
    return get_check_class(spec.type)(spec)


def available_checks() -> list[str]:
    _load_plugins()
    return sorted(_REGISTRY)


def describe_checks() -> list[dict[str, Any]]:
    """Machine-readable catalog of check types - powers docs and MCP discovery."""
    _load_plugins()
    described = []
    for name, cls in sorted(_REGISTRY.items()):
        described.append(
            {
                "type": name,
                "summary": cls.summary or (cls.__doc__ or "").strip().split("\n")[0],
                "requires_dataset": cls.requires_dataset,
                "requires_column": cls.requires_column,
                "params": list(cls.supported_params),
                "aliases": sorted(a for a, target in _ALIASES.items() if target == name),
            }
        )
    return described


#: Convenience for check modules that prefer a factory over a decorator.
CheckFactory = Callable[[CheckSpec], Check]
