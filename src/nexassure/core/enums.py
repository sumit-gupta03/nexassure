# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Enumerations shared across the domain model."""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    """How much a failing check matters.

    Severity decides the status a failure maps to and whether the run exits
    non-zero. WARN failures never break a pipeline.
    """

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def blocking(self) -> bool:
        return self in (Severity.ERROR, Severity.CRITICAL)


class CheckStatus(str, Enum):
    """Outcome of a single check."""

    PASSED = "passed"
    FAILED = "failed"
    WARNED = "warned"
    ERRORED = "errored"
    SKIPPED = "skipped"

    @property
    def ok(self) -> bool:
        return self in (CheckStatus.PASSED, CheckStatus.SKIPPED)


class RunStatus(str, Enum):
    """Outcome of a whole suite run."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    CANCELLED = "cancelled"


class ColumnKind(str, Enum):
    """Normalised type family, so profiling stays dialect-independent."""

    NUMERIC = "numeric"
    STRING = "string"
    BOOLEAN = "boolean"
    TEMPORAL = "temporal"
    BINARY = "binary"
    JSON = "json"
    OTHER = "other"


class Operator(str, Enum):
    """Comparison operators available to expectations."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    BETWEEN = "between"
    IN = "in"
    NOT_IN = "not_in"
    MATCHES = "matches"
    NOT_MATCHES = "not_matches"
    CONTAINS = "contains"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    EMPTY = "empty"
    NOT_EMPTY = "not_empty"
    SET_EQUALS = "set_equals"
    ROWS_EQUAL = "rows_equal"
    APPROX = "approx"


class ResultShape(str, Enum):
    """How to reduce a custom query result set before comparing."""

    SCALAR = "scalar"
    ROW = "row"
    COLUMN = "column"
    TABLE = "table"
    ROW_COUNT = "row_count"
