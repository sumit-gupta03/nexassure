# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""The NexAssure domain model.

Everything the user declares (connections, suites, checks, expectations) and
everything NexAssure produces (results, profiles, runs) is a Pydantic model, so the
YAML loader, the REST API and the MCP server all share one validated schema.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import CheckStatus, ColumnKind, Operator, ResultShape, RunStatus, Severity

_ENV_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def resolve_env(value: str) -> str:
    """Expand ``${env:VAR}`` / ``${env:VAR:-default}`` references in a string.

    Secrets stay in the environment (or a mounted file) rather than in the YAML
    that gets committed to git.
    """

    def _sub(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        found = os.getenv(name)
        if found is not None:
            return found
        if default is not None:
            return default
        raise KeyError(f"Environment variable {name!r} referenced in config is not set")

    return _ENV_RE.sub(_sub, value)


class _Base(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        use_enum_values=False,
        str_strip_whitespace=True,
    )


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #


class ConnectionConfig(_Base):
    """A data source NexAssure can test against.

    Only ``name`` and ``type`` are universally required; each connector decides
    which of the remaining fields it needs and validates them on connect.
    A raw ``dsn`` always wins over the individual fields.
    """

    name: str = Field(..., description="Unique key referenced by suites, e.g. prod_snowflake")
    type: str = Field(..., description="Connector id: snowflake, postgres, mssql, ...")
    description: str | None = None

    dsn: str | None = Field(None, description="Full SQLAlchemy URL; overrides discrete fields")
    host: str | None = None
    port: int | None = None
    database: str | None = None
    db_schema: str | None = Field(None, alias="schema", description="Default schema")
    username: str | None = None
    password: str | None = Field(None, repr=False)

    # Warehouse-specific
    account: str | None = Field(None, description="Snowflake account identifier")
    warehouse: str | None = Field(None, description="Snowflake virtual warehouse")
    role: str | None = None
    service_name: str | None = Field(None, description="Oracle service name")
    driver: str | None = Field(None, description="ODBC driver name for MSSQL/Synapse")
    private_key_path: str | None = Field(None, description="Snowflake key-pair auth")
    authenticator: str | None = None

    connect_args: dict[str, Any] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict, alias="params")

    connect_timeout: int = 30
    query_timeout: int = Field(600, description="Per-statement timeout in seconds")
    pool_size: int = 5
    max_overflow: int = 5
    readonly: bool = Field(True, description="Reject non-SELECT statements on this connection")

    tags: list[str] = Field(default_factory=list)

    @field_validator(
        "dsn",
        "host",
        "database",
        "db_schema",
        "username",
        "password",
        "account",
        "warehouse",
        "role",
        "service_name",
        "driver",
        "private_key_path",
        "authenticator",
        mode="before",
    )
    @classmethod
    def _expand(cls, value: Any) -> Any:
        return resolve_env(value) if isinstance(value, str) else value

    @field_validator("connect_args", "query_params", mode="before")
    @classmethod
    def _expand_mapping(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: (resolve_env(v) if isinstance(v, str) else v) for k, v in value.items()}
        return value

    @property
    def safe_summary(self) -> dict[str, Any]:
        """Connection details with secrets removed - safe to log or return over MCP."""
        data = self.model_dump(
            exclude={"password", "connect_args", "private_key_path"}, by_alias=True, mode="json"
        )
        data["password"] = "***" if self.password else None
        return data

    def fingerprint(self) -> str:
        """Stable hash of the target (not the credentials), used as a metastore key."""
        parts = [
            self.type,
            self.account or "",
            self.host or "",
            str(self.port or ""),
            self.database or "",
            self.db_schema or "",
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #


class TableRef(_Base):
    """A fully-qualified table or view reference."""

    database: str | None = None
    db_schema: str | None = Field(None, alias="schema")
    table: str

    @classmethod
    def parse(cls, raw: str) -> TableRef:
        """Parse ``db.schema.table`` / ``schema.table`` / ``table``.

        Quoted identifiers keep their case and may contain dots, so
        ``"my db"."odd.schema".tbl`` parses into three parts.
        """
        parts: list[str] = []
        current = ""
        in_quote = False
        for ch in raw:
            if ch == '"':
                in_quote = not in_quote
            elif ch == "." and not in_quote:
                parts.append(current)
                current = ""
            else:
                current += ch
        parts.append(current)
        parts = [p.strip() for p in parts if p.strip()]
        if not parts:
            raise ValueError(f"Cannot parse table reference from {raw!r}")
        if len(parts) == 1:
            return cls(table=parts[0])
        if len(parts) == 2:
            return cls(schema=parts[0], table=parts[1])
        return cls(database=parts[-3], schema=parts[-2], table=parts[-1])

    @property
    def fqn(self) -> str:
        return ".".join(p for p in (self.database, self.db_schema, self.table) if p)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.fqn


class ColumnInfo(_Base):
    """A column as discovered by connector introspection."""

    name: str
    data_type: str
    kind: ColumnKind = ColumnKind.OTHER
    nullable: bool = True
    primary_key: bool = False
    ordinal: int = 0
    default: str | None = None
    comment: str | None = None
    character_maximum_length: int | None = None
    numeric_precision: int | None = None
    numeric_scale: int | None = None


class DatasetInfo(_Base):
    """A table/view plus its columns - what lands in the metastore on connect."""

    ref: TableRef
    object_type: Literal["table", "view", "materialized_view", "external_table"] = "table"
    columns: list[ColumnInfo] = Field(default_factory=list)
    row_count: int | None = None
    size_bytes: int | None = None
    comment: str | None = None
    discovered_at: datetime = Field(default_factory=utcnow)

    def column(self, name: str) -> ColumnInfo | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)


# --------------------------------------------------------------------------- #
# Expectations
# --------------------------------------------------------------------------- #


class Expectation(_Base):
    """Declares what a custom query output should be.

    ``shape`` reduces the result set (a single value, one row, one column, the
    whole table, or just its row count) and ``operator`` compares that reduction
    to ``value``. The defaults - ``shape: scalar``, ``operator: eq`` - cover the
    common "this query should return 0" case.
    """

    shape: ResultShape = ResultShape.SCALAR
    operator: Operator = Operator.EQ
    value: Any = None
    tolerance: float = Field(0.0, ge=0.0, description="Absolute tolerance for numeric compares")
    relative_tolerance: float = Field(
        0.0, ge=0.0, description="Fractional tolerance, e.g. 0.01 means 1 percent"
    )
    ignore_row_order: bool = True
    ignore_case: bool = False
    case_sensitive_columns: bool = False

    @model_validator(mode="after")
    def _check_operator_arity(self) -> Expectation:
        needs_value = {
            Operator.EQ,
            Operator.NE,
            Operator.GT,
            Operator.GTE,
            Operator.LT,
            Operator.LTE,
            Operator.BETWEEN,
            Operator.IN,
            Operator.NOT_IN,
            Operator.MATCHES,
            Operator.NOT_MATCHES,
            Operator.CONTAINS,
            Operator.SET_EQUALS,
            Operator.ROWS_EQUAL,
            Operator.APPROX,
        }
        if self.operator in needs_value and self.value is None:
            raise ValueError(f"operator {self.operator.value!r} requires a 'value'")
        if self.operator is Operator.BETWEEN and (
            not isinstance(self.value, (list, tuple)) or len(self.value) != 2
        ):
            raise ValueError("operator 'between' requires value to be [low, high]")
        return self


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


class CheckSpec(_Base):
    """A single declared data test.

    A check is either built-in (``type: not_null``, ``type: unique``, ...), in
    which case NexAssure generates the SQL, or custom (``type: custom_sql``), where
    the user supplies ``query`` and an ``expect`` block.
    """

    name: str = Field(..., description="Unique within its suite")
    type: str = Field(..., description="Registered check type id")
    description: str | None = Field(None, description="Business meaning; shown in reports")

    dataset: TableRef | None = Field(None, description="Target table; omit for free-form SQL")
    column: str | None = None
    columns: list[str] = Field(default_factory=list)

    query: str | None = Field(None, description="Custom SELECT for type: custom_sql")
    expect: Expectation | None = None

    params: dict[str, Any] = Field(default_factory=dict, description="Check-type specific options")
    where: str | None = Field(None, description="SQL predicate applied to the target table")

    severity: Severity = Severity.ERROR
    threshold: float | None = Field(
        None,
        ge=0.0,
        description="Tolerated failure ratio (0-1) or absolute count (>1) before the check fails",
    )
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    timeout: int | None = None
    sample_limit: int = Field(10, ge=0, le=1000, description="Failing rows captured for triage")
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("dataset", mode="before")
    @classmethod
    def _parse_dataset(cls, value: Any) -> Any:
        return TableRef.parse(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _normalise_columns(self) -> CheckSpec:
        if self.column and not self.columns:
            self.columns = [self.column]
        elif self.columns and not self.column:
            self.column = self.columns[0]
        return self

    @property
    def check_id(self) -> str:
        """Deterministic id, so results for the same check line up across runs."""
        seed = "|".join(
            [
                self.name,
                self.type,
                self.dataset.fqn if self.dataset else "",
                ",".join(self.columns),
            ]
        )
        return hashlib.sha256(seed.encode()).hexdigest()[:16]


class SuiteDefaults(_Base):
    """Values inherited by every check in a suite unless overridden."""

    severity: Severity = Severity.ERROR
    threshold: float | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    db_schema: str | None = Field(None, alias="schema")
    database: str | None = None
    sample_limit: int = 10
    timeout: int | None = None


class Suite(_Base):
    """A named, versioned collection of checks bound to one connection."""

    name: str
    connection: str = Field(..., description="Name of a ConnectionConfig")
    description: str | None = None
    version: str = "1"
    defaults: SuiteDefaults = Field(default_factory=SuiteDefaults)
    checks: list[CheckSpec] = Field(default_factory=list)
    schedule: str | None = Field(None, description="Cron expression, 5 or 6 fields")
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    max_parallel: int = Field(8, ge=1, le=64)
    fail_fast: bool = False
    source_path: str | None = Field(None, description="File the suite was loaded from")

    @model_validator(mode="after")
    def _apply_defaults(self) -> Suite:
        seen: set[str] = set()
        for check in self.checks:
            if check.name in seen:
                raise ValueError(f"Duplicate check name {check.name!r} in suite {self.name!r}")
            seen.add(check.name)

            if "severity" not in check.model_fields_set:
                check.severity = self.defaults.severity
            if check.threshold is None:
                check.threshold = self.defaults.threshold
            if check.owner is None:
                check.owner = self.defaults.owner
            if check.timeout is None:
                check.timeout = self.defaults.timeout
            if "sample_limit" not in check.model_fields_set:
                check.sample_limit = self.defaults.sample_limit
            if self.defaults.tags:
                check.tags = sorted(set(check.tags) | set(self.defaults.tags))
            if check.dataset is not None:
                if check.dataset.db_schema is None and self.defaults.db_schema:
                    check.dataset.db_schema = self.defaults.db_schema
                if check.dataset.database is None and self.defaults.database:
                    check.dataset.database = self.defaults.database
        return self

    def select(
        self,
        names: list[str] | None = None,
        tags: list[str] | None = None,
        datasets: list[str] | None = None,
    ) -> list[CheckSpec]:
        """Filter checks by name / tag / dataset - powers ``nexassure run --select``."""
        chosen = [c for c in self.checks if c.enabled]
        if names:
            wanted = set(names)
            chosen = [c for c in chosen if c.name in wanted]
        if tags:
            wanted = set(tags)
            chosen = [c for c in chosen if wanted & set(c.tags)]
        if datasets:
            wanted = {d.lower() for d in datasets}
            chosen = [c for c in chosen if c.dataset and c.dataset.fqn.lower() in wanted]
        return chosen


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


class CheckResult(_Base):
    """The outcome of executing one :class:`CheckSpec`."""

    check_id: str
    check_name: str
    check_type: str
    status: CheckStatus
    severity: Severity = Severity.ERROR

    description: str | None = None
    dataset: str | None = None
    column: str | None = None

    observed: Any = None
    expected: Any = None
    message: str = ""

    rows_scanned: int | None = None
    rows_failed: int | None = None
    failed_ratio: float | None = None
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)

    query: str | None = Field(None, description="SQL actually executed")
    duration_ms: float = 0.0
    started_at: datetime = Field(default_factory=utcnow)
    error: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None

    @property
    def passed(self) -> bool:
        return self.status.ok


class RunSummary(_Base):
    """Aggregate counters for a run."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    warned: int = 0
    errored: int = 0
    skipped: int = 0

    @property
    def pass_rate(self) -> float:
        considered = self.total - self.skipped
        return 1.0 if considered <= 0 else self.passed / considered


class RunResult(_Base):
    """Everything produced by one suite execution."""

    run_id: str = Field(default_factory=new_id)
    suite_name: str
    connection_name: str
    status: RunStatus = RunStatus.PENDING
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    duration_ms: float = 0.0
    results: list[CheckResult] = Field(default_factory=list)
    summary: RunSummary = Field(default_factory=RunSummary)
    error: str | None = None
    triggered_by: str = "manual"
    environment: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def recompute(self) -> RunResult:
        """Refresh ``summary`` and ``status`` from ``results``."""
        s = RunSummary(total=len(self.results))
        counters = {
            CheckStatus.PASSED: "passed",
            CheckStatus.FAILED: "failed",
            CheckStatus.WARNED: "warned",
            CheckStatus.ERRORED: "errored",
            CheckStatus.SKIPPED: "skipped",
        }
        for r in self.results:
            attr = counters[r.status]
            setattr(s, attr, getattr(s, attr) + 1)
        self.summary = s
        if self.status is RunStatus.CANCELLED:
            return self
        if s.errored:
            self.status = RunStatus.ERRORED
        elif s.failed:
            self.status = RunStatus.FAILED
        else:
            self.status = RunStatus.PASSED
        return self

    @property
    def exit_code(self) -> int:
        """0 when nothing blocking failed - the value ``nexassure run`` returns to CI."""
        return 1 if self.status in (RunStatus.ERRORED, RunStatus.FAILED) else 0

    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status in (CheckStatus.FAILED, CheckStatus.ERRORED)]


# --------------------------------------------------------------------------- #
# Profiling
# --------------------------------------------------------------------------- #


class ColumnProfile(_Base):
    """Per-column statistics from a profiling pass."""

    column: str
    data_type: str
    kind: ColumnKind = ColumnKind.OTHER

    row_count: int = 0
    null_count: int = 0
    null_ratio: float = 0.0
    distinct_count: int | None = None
    distinct_ratio: float | None = None
    duplicate_count: int | None = None
    is_unique: bool | None = None
    blank_count: int | None = Field(None, description="Empty or whitespace-only strings")
    zero_count: int | None = None

    min: Any = None
    max: Any = None
    mean: float | None = None
    stddev: float | None = None
    sum: float | None = None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    p95: float | None = None

    min_length: int | None = None
    max_length: int | None = None
    avg_length: float | None = None

    top_values: list[dict[str, Any]] = Field(default_factory=list)
    completeness: float = 1.0

    @property
    def suggested_not_null(self) -> bool:
        return self.null_count == 0 and self.row_count > 0

    @property
    def suggested_unique(self) -> bool:
        return bool(self.is_unique) and self.row_count > 0


class TableProfile(_Base):
    """A profiling snapshot of one table."""

    profile_id: str = Field(default_factory=new_id)
    dataset: TableRef
    connection_name: str
    row_count: int = 0
    column_count: int = 0
    duplicate_row_count: int | None = None
    size_bytes: int | None = None
    columns: list[ColumnProfile] = Field(default_factory=list)
    sampled: bool = False
    sample_size: int | None = None
    profiled_at: datetime = Field(default_factory=utcnow)
    duration_ms: float = 0.0

    def column_profile(self, name: str) -> ColumnProfile | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.column.lower() == lowered), None)


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
    "ResultShape",
    "RunResult",
    "RunStatus",
    "RunSummary",
    "Severity",
    "Suite",
    "SuiteDefaults",
    "TableProfile",
    "TableRef",
    "new_id",
    "resolve_env",
    "utcnow",
]
