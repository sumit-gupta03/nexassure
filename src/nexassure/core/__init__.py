# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Domain model, enums and the execution engine.

``SuiteRunner`` lives in :mod:`nexassure.core.engine`, which imports the check
registry, which in turn imports connectors, which import ``core.enums``.
Importing the engine eagerly here would close that loop, so the two engine
symbols are resolved lazily through :pep:`562` module ``__getattr__``. They stay
importable as ``from nexassure.core import SuiteRunner``; they are just not
imported until first touched.
"""

from typing import Any

from .enums import CheckStatus, ColumnKind, Operator, ResultShape, RunStatus, Severity
from .models import (
    CheckResult,
    CheckSpec,
    ColumnInfo,
    ColumnProfile,
    ConnectionConfig,
    DatasetInfo,
    Expectation,
    RunResult,
    RunSummary,
    Suite,
    SuiteDefaults,
    TableProfile,
    TableRef,
)

_LAZY = {"SuiteRunner": "engine", "plan_waves": "engine", "ProgressCallback": "engine"}


def __getattr__(name: str) -> Any:
    """Resolve engine symbols on first access, avoiding an import cycle."""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache, so later lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "CheckResult",
    "CheckSpec",
    "CheckStatus",
    "ColumnInfo",
    "ColumnKind",
    "ColumnProfile",
    "ConnectionConfig",
    "DatasetInfo",
    "Expectation",
    "Operator",
    "ProgressCallback",
    "ResultShape",
    "RunResult",
    "RunStatus",
    "RunSummary",
    "Severity",
    "Suite",
    "SuiteDefaults",
    "SuiteRunner",
    "TableProfile",
    "TableRef",
    "plan_waves",
]
