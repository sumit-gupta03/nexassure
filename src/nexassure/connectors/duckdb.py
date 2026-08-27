# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""DuckDB connector.

DuckDB is the local-development and CI target: the whole test suite runs against
it with no server, and it reads Parquet/CSV directly, so ``nexassure profile`` works
on a file lying in a data lake bucket without a warehouse in the loop.
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy.engine import URL

from .base import BaseConnector, Dialect


class DuckDBDialect(Dialect):
    supports_approx_count_distinct = True
    supports_tablesample = True
    limit_style = "limit"

    def approx_count_distinct(self, expr: str) -> str:
        return f"APPROX_COUNT_DISTINCT({expr})"

    def regexp_match(self, expr: str, pattern_literal: str) -> str:
        return f"REGEXP_MATCHES({expr}, {pattern_literal})"

    def percentile(self, expr: str, fraction: float) -> str:
        return f"QUANTILE_CONT({expr}, {fraction})"

    def hours_between(self, later: str, earlier: str) -> str:
        return f"(DATE_DIFF('second', {earlier}, {later}) / 3600.0)"

    def cast_varchar(self, expr: str) -> str:
        return f"CAST({expr} AS VARCHAR)"


class DuckDBConnector(BaseConnector):
    """DuckDB, in-memory or file-backed."""

    name: ClassVar[str] = "duckdb"
    extra: ClassVar[str] = "duckdb"
    driver_module: ClassVar[str] = "duckdb_engine"
    dialect_class: ClassVar[type[Dialect]] = DuckDBDialect
    serialized_access: ClassVar[bool] = True

    def build_url(self) -> str | URL:
        if self.config.dsn:
            return self.config.dsn
        # ``database`` is a filesystem path here; omitting it gives an in-memory DB.
        path = self.config.database or ":memory:"
        return f"duckdb:///{path}"

    def connect(self) -> BaseConnector:
        """Create a single-connection engine.

        DuckDB allows one writer per file and scopes an in-memory database to
        its connection, so a normal pool would hand out connections that cannot
        see each other tables.

        ``StaticPool`` - one DBAPI connection shared by every thread - is used
        rather than ``SingletonThreadPool``, which closes a connection when its
        owning thread exits. The parallel check executor retires worker threads
        between waves, and the resulting "Connection already closed" errors
        surfaced as spurious ERRORED checks. DuckDB serialises access to a
        shared connection internally, so sharing one is safe; concurrent checks
        simply queue behind it, which is the right trade for a local engine.
        """
        if self._engine is not None:
            return self
        self._check_driver()
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        self._engine = create_engine(
            self.build_url(),
            connect_args=self.build_connect_args(),
            poolclass=StaticPool,
            future=True,
        )
        return self

    def list_schemas(self) -> list[str]:
        result = self.execute(
            "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name",
            max_rows=None,
        )
        return [row[0] for row in result.rows]

    @property
    def catalog_name(self) -> str | None:
        """Always None: ``database`` is a file path here, not a SQL catalog."""
        return None

    def server_version(self) -> str | None:
        try:
            return str(self.scalar("SELECT version()"))
        except Exception:
            return None
