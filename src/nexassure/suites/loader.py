# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Suite loading and validation.

Suites are YAML. A file holds one suite, or several under a top-level
``suites:`` key, or a bare list of suites - all three shapes are accepted
because people organise repos differently and none of the variants is
ambiguous.

Validation happens in two passes:

1. **Structural** - Pydantic checks required fields, types and enum values.
2. **Semantic** - :func:`validate_suite` catches mistakes Pydantic cannot see:
   unknown check types, dependencies on checks that do not exist, dependency
   cycles, and per-check-type requirements.

Both run before any warehouse connection is opened, so ``nexassure validate`` is a
fast, offline lint suitable for a pre-commit hook.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from ..checks.base import available_checks, get_check_class
from ..core.models import Suite
from ..exceptions import SuiteError, UnknownCheckType
from ..logging_conf import get_logger

log = get_logger(__name__)


def load_suite_file(path: str | Path) -> list[Suite]:
    """Parse one YAML file into suites."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise SuiteError(f"Suite file not found: {resolved}", path=str(resolved))

    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SuiteError(f"Could not parse {resolved}: {exc}", path=str(resolved)) from exc

    if raw is None:
        return []

    documents = _normalise_documents(raw, resolved)
    suites: list[Suite] = []
    for document in documents:
        try:
            suite = Suite(**document)
        except Exception as exc:
            name = document.get("name", "<unnamed>") if isinstance(document, dict) else "<invalid>"
            raise SuiteError(
                f"Invalid suite {name!r} in {resolved}: {exc}", path=str(resolved), suite=name
            ) from exc
        suite.source_path = str(resolved)
        suites.append(suite)
    return suites


def _normalise_documents(raw: Any, path: Path) -> list[dict[str, Any]]:
    """Accept the three legal file shapes and reject anything else clearly."""
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, dict):
        if "suites" in raw and isinstance(raw["suites"], list):
            return list(raw["suites"])
        return [raw]
    raise SuiteError(
        f"{path} must contain a suite mapping, a list of suites, or a top-level 'suites' key",
        path=str(path),
    )


def load_suites(paths: Iterable[str | Path]) -> list[Suite]:
    """Load several suite files, rejecting duplicate suite names.

    Duplicate names would make ``--suite`` ambiguous and would collide in the
    metastore, so this fails loudly rather than picking one.
    """
    suites: list[Suite] = []
    origins: dict[str, str] = {}

    for path in paths:
        for suite in load_suite_file(path):
            if suite.name in origins:
                raise SuiteError(
                    f"Duplicate suite name {suite.name!r} in {suite.source_path} "
                    f"(already defined in {origins[suite.name]})",
                    suite=suite.name,
                )
            origins[suite.name] = suite.source_path or str(path)
            suites.append(suite)
    return suites


def discover_suites(root: str | Path, patterns: Iterable[str] | None = None) -> list[Suite]:
    """Find and load every suite under ``root``."""
    base = Path(root).expanduser()
    globs = list(patterns or ("**/*.yml", "**/*.yaml"))
    files: list[Path] = []
    seen: set[Path] = set()
    for pattern in globs:
        for match in sorted(base.glob(pattern)):
            resolved = match.resolve()
            if match.is_file() and resolved not in seen:
                seen.add(resolved)
                files.append(match)
    return load_suites(files)


def validate_suite(suite: Suite) -> list[str]:
    """Return a list of problems with a suite. Empty means it is valid.

    Collects every problem rather than stopping at the first, so one
    ``nexassure validate`` run tells the user everything they need to fix.
    """
    problems: list[str] = []
    names = {check.name for check in suite.checks}

    if not suite.checks:
        problems.append(f"Suite {suite.name!r} declares no checks")

    for check in suite.checks:
        label = f"{suite.name}.{check.name}"

        try:
            check_class = get_check_class(check.type)
        except UnknownCheckType:
            problems.append(
                f"{label}: unknown check type {check.type!r}. "
                f"Available types: {', '.join(available_checks())}"
            )
            continue

        if check_class.requires_dataset and check.dataset is None:
            problems.append(f"{label}: type {check.type!r} requires a 'dataset'")
        if check_class.requires_column and not check.columns:
            problems.append(f"{label}: type {check.type!r} requires a 'column'")

        unknown_params = set(check.params) - set(check_class.supported_params)
        if unknown_params and check_class.supported_params:
            problems.append(
                f"{label}: unsupported param(s) {', '.join(sorted(unknown_params))}. "
                f"Supported: {', '.join(check_class.supported_params)}"
            )

        if (
            check.type in ("custom_sql", "sql_returns_no_rows", "sql_returns_rows")
            and not check.query
        ):
            problems.append(f"{label}: type {check.type!r} requires a 'query'")

        if check.threshold is not None and check.threshold < 0:
            problems.append(f"{label}: threshold must not be negative")

        for dependency in check.depends_on:
            if dependency not in names:
                problems.append(f"{label}: depends_on references unknown check {dependency!r}")

    problems.extend(_detect_cycles(suite))
    return problems


def _detect_cycles(suite: Suite) -> list[str]:
    """Find dependency cycles with an iterative depth-first search.

    Iterative rather than recursive so a pathological suite cannot blow the
    Python stack.
    """
    graph = {check.name: list(check.depends_on) for check in suite.checks}
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(graph, white)
    problems: list[str] = []

    for start in graph:
        if colour[start] != white:
            continue
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            if colour.get(node) == grey:
                colour[node] = black
                continue
            if colour.get(node) == black:
                continue
            colour[node] = grey
            stack.append((node, path))
            for neighbour in graph.get(node, []):
                if neighbour not in graph:
                    continue  # already reported as an unknown dependency
                if colour[neighbour] == grey:
                    cycle = " -> ".join([*path, neighbour])
                    problems.append(f"Dependency cycle in suite {suite.name!r}: {cycle}")
                elif colour[neighbour] == white:
                    stack.append((neighbour, [*path, neighbour]))
    return problems


def validate_suites(suites: Iterable[Suite]) -> dict[str, list[str]]:
    """Validate several suites. Returns ``{suite_name: problems}`` for failures only."""
    report: dict[str, list[str]] = {}
    for suite in suites:
        problems = validate_suite(suite)
        if problems:
            report[suite.name] = problems
    return report


def dump_suite(suite: Suite, path: str | Path | None = None) -> str:
    """Serialise a suite back to YAML.

    Used by ``nexassure suggest`` to write a generated suite. Empty and default
    fields are dropped so the output reads like something a person wrote.
    """
    payload = suite.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude_defaults=True,
        exclude={"source_path"},
    )

    # exclude_defaults would drop 'name' and 'type' if they matched a default,
    # and drops checks entirely when the list is short; restore what must stay.
    payload["name"] = suite.name
    payload["connection"] = suite.connection
    payload["checks"] = [
        {
            **check.model_dump(
                mode="json", by_alias=True, exclude_none=True, exclude_defaults=True
            ),
            "name": check.name,
            "type": check.type,
        }
        for check in suite.checks
    ]
    for rendered, check in zip(payload["checks"], suite.checks, strict=True):
        # 'columns' duplicates 'column' for single-column checks; keep it terse.
        if rendered.get("columns") == [check.column]:
            rendered.pop("columns", None)
        if check.dataset is not None:
            rendered["dataset"] = check.dataset.fqn

    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100)
    header = (
        "# Generated by NexAssure. Review before promoting to severity 'error'.\n"
        "# Docs: https://github.com/sumit-gupta03/nexassure/tree/main/docs\n"
    )
    output = header + text

    if path is not None:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
        log.info("Wrote suite %r to %s", suite.name, target)
    return output
