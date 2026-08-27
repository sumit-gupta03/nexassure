# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Connector base class and SQL dialect abstraction.

A connector wraps one SQLAlchemy engine and answers three kinds of question:

1. **Execution** - run this SQL, give me rows.
2. **Introspection** - what schemas, tables and columns exist here?
3. **Dialect** - how does *this* warehouse spell ``stddev``, regex matching,
   percentiles, ``LIMIT``, and identifier quoting?

Point 3 is what lets a single ``not_null`` check definition run unchanged on
Snowflake, Postgres, Redshift, Synapse, Oracle and SQL Server. Subclasses
override only the fragments that actually differ.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from ..core.enums import ColumnKind
from ..core.models import ColumnInfo, ConnectionConfig, DatasetInfo, TableRef
from ..exceptions import ConnectionError_, DriverNotInstalled
from ..logging_conf import get_logger
from ..utils.sqlsafe import assert_readonly

log = get_logger(__name__)


@dataclass(slots=True)
class QueryResult:
    """Rows returned by :meth:`BaseConnector.execute`."""

    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    duration_ms: float = 0.0
    sql: str = ""
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def scalar(self) -> Any:
        """First column of the first row, or ``None`` when the result is empty."""
        if not self.rows or not self.rows[0]:
            return None
        return self.rows[0][0]

    def first(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def column_values(self, index: int = 0) -> list[Any]:
        return [r[index] for r in self.rows if len(r) > index]

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]

    def is_empty(self) -> bool:
        return not self.rows


class Dialect:
    """SQL fragments that vary between engines.

    Defaults follow ANSI SQL. Each connector subclasses this and overrides only
    what its engine does differently, which keeps the per-warehouse surface area
    small enough to review.
    """

    #: Character pair used to quote identifiers.
    quote_char: ClassVar[tuple[str, str]] = ('"', '"')
    #: ``True`` when unquoted identifiers fold to upper case (Snowflake, Oracle).
    folds_upper: ClassVar[bool] = False
    supports_stddev: ClassVar[bool] = True
    supports_percentile: ClassVar[bool] = True
    supports_regexp: ClassVar[bool] = True
    supports_approx_count_distinct: ClassVar[bool] = False
    supports_tablesample: ClassVar[bool] = True
    #: Some engines (Oracle 11-, older Synapse) reject ``LIMIT``.
    limit_style: ClassVar[str] = "limit"  # limit | top | fetch_first

    def quote(self, identifier: str) -> str:
        """Quote a single identifier, escaping any embedded quote characters."""
        open_q, close_q = self.quote_char
        escaped = identifier.replace(close_q, close_q * 2)
        return f"{open_q}{escaped}{close_q}"

    def qualify(self, ref: TableRef) -> str:
        """Render a fully-qualified, safely-quoted table name."""
        parts = [p for p in (ref.database, ref.db_schema, ref.table) if p]
        return ".".join(self.quote(p) for p in parts)

    def limit(self, sql: str, n: int) -> str:
        """Wrap a SELECT so it returns at most ``n`` rows."""
        if n <= 0:
            return sql
        if self.limit_style == "top":
            lowered = sql.lstrip()
            if lowered[:6].lower() == "select":
                return f"SELECT TOP {n}" + lowered[6:]
            return f"SELECT TOP {n} * FROM ({sql}) AS _na_limited"
        if self.limit_style == "fetch_first":
            return f"{sql} FETCH FIRST {n} ROWS ONLY"
        return f"{sql} LIMIT {n}"

    # -- expression builders ------------------------------------------------ #

    def stddev(self, expr: str) -> str:
        return f"STDDEV_SAMP({expr})"

    def percentile(self, expr: str, fraction: float) -> str:
        return f"PERCENTILE_CONT({fraction}) WITHIN GROUP (ORDER BY {expr})"

    def regexp_match(self, expr: str, pattern_literal: str) -> str:
        """Boolean expression: does ``expr`` match the regex in ``pattern_literal``?"""
        return f"REGEXP_LIKE({expr}, {pattern_literal})"

    def length(self, expr: str) -> str:
        return f"LENGTH({expr})"

    def trim(self, expr: str) -> str:
        return f"TRIM({expr})"

    def cast_double(self, expr: str) -> str:
        return f"CAST({expr} AS DOUBLE PRECISION)"

    def cast_varchar(self, expr: str) -> str:
        return f"CAST({expr} AS VARCHAR(4000))"

    def current_timestamp(self) -> str:
        return "CURRENT_TIMESTAMP"

    def hours_between(self, later: str, earlier: str) -> str:
        """Fractional hours from ``earlier`` to ``later``."""
        return f"(EXTRACT(EPOCH FROM ({later} - {earlier})) / 3600.0)"

    def string_literal(self, value: str) -> str:
        """Render a Python string as a SQL literal, doubling embedded quotes."""
        return "'" + str(value).replace("'", "''") + "'"

    def normalise_identifier(self, name: str) -> str:
        """Case-fold an identifier the way an unquoted one would be stored."""
        return name.upper() if self.folds_upper else name.lower()


#: Rough type-name to :class:`ColumnKind` mapping, shared by all connectors.
_TYPE_FAMILIES: list[tuple[ColumnKind, tuple[str, ...]]] = [
    (
        ColumnKind.NUMERIC,
        (
            "int",
            "serial",
            "numeric",
            "decimal",
            "float",
            "double",
            "real",
            "money",
            "number",
            "bigint",
            "smallint",
            "tinyint",
            "bit",
            "long",
        ),
    ),
    (ColumnKind.TEMPORAL, ("date", "time", "timestamp", "datetime", "interval", "year")),
    (ColumnKind.BOOLEAN, ("bool",)),
    (ColumnKind.JSON, ("json", "jsonb", "variant", "object", "struct", "map")),
    (ColumnKind.BINARY, ("blob", "binary", "bytea", "varbinary", "raw", "image")),
    (
        ColumnKind.STRING,
        (
            "char",
            "text",
            "string",
            "varchar",
            "nvarchar",
            "nchar",
            "clob",
            "uuid",
            "enum",
        ),
    ),
]


def classify_type(data_type: str) -> ColumnKind:
    """Map a warehouse type name onto a dialect-independent family.

    BOOLEAN is checked before NUMERIC so that SQL Server ``bit`` does not get
    mistaken for a number, and JSON before STRING so Snowflake ``VARIANT`` is
    not profiled as text.
    """
    lowered = (data_type or "").lower()
    for kind in (
        ColumnKind.BOOLEAN,
        ColumnKind.JSON,
        ColumnKind.TEMPORAL,
        ColumnKind.BINARY,
        ColumnKind.NUMERIC,
        ColumnKind.STRING,
    ):
        needles = next(n for k, n in _TYPE_FAMILIES if k is kind)
        if any(n in lowered for n in needles):
            return kind
    return ColumnKind.OTHER


class BaseConnector(ABC):
    """One connection to one data source.

    Connectors are context managers::

        with PostgresConnector(cfg) as db:
            print(db.execute("SELECT 1").scalar())
    """

    #: Registry key, matched against ``ConnectionConfig.type``.
    name: ClassVar[str] = "base"
    #: pip extra that provides the driver, quoted in the install hint on failure.
    extra: ClassVar[str] = ""
    #: Python module that must import for the connector to work.
    driver_module: ClassVar[str | None] = None
    dialect_class: ClassVar[type[Dialect]] = Dialect
    default_port: ClassVar[int | None] = None
    #: Set by engines that share one physical connection across threads.
    #:
    #: DuckDB and file-backed SQLite allow a single connection to the database,
    #: so the pool hands the same DBAPI connection to every worker. SQLAlchemy
    #: opens a transaction per checkout, and concurrent checks then collide with
    #: "cannot start a transaction within a transaction". Serialising access
    #: costs nothing on a local engine and keeps real warehouses fully parallel.
    serialized_access: ClassVar[bool] = False

    def __init__(self, config: ConnectionConfig) -> None:
        self.config = config
        self.dialect = self.dialect_class()
        self._engine: Engine | None = None
        self._access_lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------- #

    def __enter__(self) -> BaseConnector:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self.connect()
        assert self._engine is not None
        return self._engine

    def _serialize(self) -> AbstractContextManager[Any]:
        """Lock around database access on single-connection engines."""
        return self._access_lock if self.serialized_access else nullcontext()

    def _check_driver(self) -> None:
        """Fail early with an actionable install hint rather than a stack trace."""
        if not self.driver_module:
            return
        import importlib

        try:
            importlib.import_module(self.driver_module)
        except ImportError as exc:
            raise DriverNotInstalled(self.name, self.extra or self.name, exc) from exc

    def connect(self) -> BaseConnector:
        """Build the engine and verify the credentials actually work."""
        if self._engine is not None:
            return self
        self._check_driver()
        try:
            self._engine = create_engine(
                self.build_url(),
                connect_args=self.build_connect_args(),
                pool_pre_ping=True,
                pool_size=self.config.pool_size,
                max_overflow=self.config.max_overflow,
                future=True,
            )
            with self._engine.connect() as conn:
                conn.execute(text(self.ping_statement()))
                for stmt in self.session_setup_statements():
                    conn.execute(text(stmt))
        except DriverNotInstalled:
            raise
        except SQLAlchemyError as exc:
            self._engine = None
            raise ConnectionError_(
                f"Could not connect to {self.config.name!r} ({self.name}): {exc}",
                connection=self.config.name,
                connector=self.name,
            ) from exc
        log.info("Connected to %s (%s)", self.config.name, self.name)
        return self

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def test_connection(self) -> dict[str, Any]:
        """Round-trip the connection and report latency - powers ``nexassure test-connection``."""
        started = time.perf_counter()
        try:
            self.connect()
            result = self.execute(self.ping_statement())
            return {
                "ok": True,
                "connection": self.config.name,
                "connector": self.name,
                "server_version": self.server_version(),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "probe_result": result.scalar(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "connection": self.config.name,
                "connector": self.name,
                "error": str(exc),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }

    # -- overridable connection details ------------------------------------- #

    @abstractmethod
    def build_url(self) -> str | URL:
        """Return the SQLAlchemy URL for this connection."""

    def build_connect_args(self) -> dict[str, Any]:
        return dict(self.config.connect_args)

    def ping_statement(self) -> str:
        return "SELECT 1"

    def session_setup_statements(self) -> Sequence[str]:
        """Statements run once per connection, e.g. setting a query timeout."""
        return ()

    def server_version(self) -> str | None:
        try:
            with self.engine.connect() as conn:
                return str(conn.exec_driver_sql("SELECT version()").scalar())
        except Exception:
            return None

    @property
    def catalog_name(self) -> str | None:
        """The database name that is usable as a SQL identifier prefix.

        On most engines this is just ``config.database``. On file-backed engines
        (DuckDB, SQLite) ``database`` holds a filesystem path, which is not a
        catalog and must never be spliced into a qualified table name - those
        connectors return ``None`` so qualification stops at the schema.
        """
        return self.config.database

    # -- execution ---------------------------------------------------------- #

    def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int | None = 10_000,
        enforce_readonly: bool | None = None,
    ) -> QueryResult:
        """Run one statement and materialise up to ``max_rows`` rows.

        Args:
            sql: The statement. Bind parameters use SQLAlchemy ``:name`` syntax.
            params: Bind values. Always prefer these over string interpolation.
            max_rows: Stop fetching past this many rows; the result is flagged
                ``truncated``. ``None`` fetches everything.
            enforce_readonly: Override the connection-level readonly setting.
        """
        readonly = self.config.readonly if enforce_readonly is None else enforce_readonly
        if readonly:
            assert_readonly(sql)

        started = time.perf_counter()
        try:
            with self._serialize(), self.engine.connect() as conn:
                cursor = conn.execute(text(sql), params or {})
                if cursor.returns_rows:
                    columns = list(cursor.keys())
                    if max_rows is None:
                        rows = [tuple(r) for r in cursor.fetchall()]
                        truncated = False
                    else:
                        fetched = cursor.fetchmany(max_rows + 1)
                        truncated = len(fetched) > max_rows
                        rows = [tuple(r) for r in fetched[:max_rows]]
                else:
                    columns, rows, truncated = [], [], False
        except SQLAlchemyError as exc:
            raise ConnectionError_(
                f"Query failed on {self.config.name!r}: {exc}",
                connection=self.config.name,
                sql=sql[:500],
            ) from exc

        return QueryResult(
            columns=columns,
            rows=rows,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            sql=sql,
            truncated=truncated,
        )

    def scalar(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        return self.execute(sql, params, max_rows=1).scalar()

    # -- introspection ------------------------------------------------------ #

    def list_schemas(self) -> list[str]:
        from sqlalchemy import inspect

        with self._serialize():
            return list(inspect(self.engine).get_schema_names())

    def list_tables(self, schema: str | None = None, include_views: bool = True) -> list[TableRef]:
        from sqlalchemy import inspect

        target = schema or self.config.db_schema
        with self._serialize():
            inspector = inspect(self.engine)
            names = list(inspector.get_table_names(schema=target))
            if include_views:
                names += list(inspector.get_view_names(schema=target))
        return [
            TableRef(database=self.catalog_name, schema=target, table=n) for n in sorted(set(names))
        ]

    def describe_table(self, ref: TableRef) -> DatasetInfo:
        """Introspect a table into a :class:`DatasetInfo`, columns included."""
        from sqlalchemy import inspect

        schema = ref.db_schema or self.config.db_schema
        with self._serialize():
            inspector = inspect(self.engine)
            try:
                raw_columns = inspector.get_columns(ref.table, schema=schema)
            except SQLAlchemyError as exc:
                raise ConnectionError_(
                    f"Table {ref.fqn!r} not found on {self.config.name!r}: {exc}",
                    dataset=ref.fqn,
                ) from exc

            pk_names: set[str] = set()
            with suppress(SQLAlchemyError):
                pk = inspector.get_pk_constraint(ref.table, schema=schema)
                pk_names = set((pk or {}).get("constrained_columns") or [])

            views: set[str] = set()
            with suppress(SQLAlchemyError):
                views = set(inspector.get_view_names(schema=schema))

        columns = [
            ColumnInfo(
                name=col["name"],
                data_type=str(col.get("type", "unknown")),
                kind=classify_type(str(col.get("type", ""))),
                nullable=bool(col.get("nullable", True)),
                primary_key=col["name"] in pk_names,
                ordinal=i,
                default=str(col["default"]) if col.get("default") is not None else None,
                comment=col.get("comment"),
            )
            for i, col in enumerate(raw_columns)
        ]
        return DatasetInfo(
            ref=TableRef(
                database=ref.database or self.catalog_name, schema=schema, table=ref.table
            ),
            object_type="view" if ref.table in views else "table",
            columns=columns,
        )

    def count_rows(self, ref: TableRef, where: str | None = None) -> int:
        sql = f"SELECT COUNT(*) FROM {self.dialect.qualify(ref)}"
        if where:
            sql += f" WHERE {where}"
        return int(self.scalar(sql) or 0)

    def sample_rows(self, ref: TableRef, limit: int = 10, where: str | None = None) -> QueryResult:
        sql = f"SELECT * FROM {self.dialect.qualify(ref)}"
        if where:
            sql += f" WHERE {where}"
        return self.execute(self.dialect.limit(sql, limit), max_rows=limit)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.config.name!r}>"
