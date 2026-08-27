# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Microsoft SQL Server connector (pyodbc).

T-SQL differs from ANSI in ways that matter to generated checks: ``TOP`` instead
of ``LIMIT``, ``LEN`` instead of ``LENGTH``, square-bracket quoting, and no
POSIX regex at all. The dialect below encodes each of those.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy.engine import URL

from .base import BaseConnector, Dialect

#: Tried in order when the config does not name an ODBC driver explicitly.
DEFAULT_ODBC_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)


def resolve_odbc_driver(preferred: str | None = None) -> str:
    """Pick an installed ODBC driver, falling back to the newest known name.

    Returning a plausible name rather than raising keeps the failure at connect
    time, where the driver error message is far more useful than ours.
    """
    if preferred:
        return preferred
    try:
        import pyodbc

        installed = {d.strip() for d in pyodbc.drivers()}
        for candidate in DEFAULT_ODBC_DRIVERS:
            if candidate in installed:
                return candidate
    except Exception:
        pass
    return DEFAULT_ODBC_DRIVERS[0]


class TSQLDialect(Dialect):
    quote_char = ("[", "]")
    supports_regexp = False
    supports_percentile = True
    supports_approx_count_distinct = True
    limit_style = "top"

    def quote(self, identifier: str) -> str:
        # T-SQL escapes a closing bracket by doubling it.
        return "[" + identifier.replace("]", "]]") + "]"

    def stddev(self, expr: str) -> str:
        return f"STDEV({expr})"

    def percentile(self, expr: str, fraction: float) -> str:
        return f"PERCENTILE_CONT({fraction}) WITHIN GROUP (ORDER BY {expr}) OVER ()"

    def approx_count_distinct(self, expr: str) -> str:
        return f"APPROX_COUNT_DISTINCT({expr})"

    def regexp_match(self, expr: str, pattern_literal: str) -> str:
        # T-SQL has no regex engine. LIKE handles the anchored wildcard cases
        # that regex checks most often express; anything richer must be written
        # as a custom_sql check.
        return f"({expr} LIKE {pattern_literal})"

    def length(self, expr: str) -> str:
        # LEN ignores trailing spaces, so DATALENGTH would over-count; LEN is
        # the right choice for the "how long is this string" question.
        return f"LEN({expr})"

    def cast_double(self, expr: str) -> str:
        return f"CAST({expr} AS FLOAT)"

    def cast_varchar(self, expr: str) -> str:
        return f"CAST({expr} AS NVARCHAR(4000))"

    def current_timestamp(self) -> str:
        return "SYSUTCDATETIME()"

    def hours_between(self, later: str, earlier: str) -> str:
        # DATEDIFF(second, ...) overflows past ~68 years; seconds is the right
        # granularity for freshness checks and stays well inside the int range.
        return f"(DATEDIFF(second, {earlier}, {later}) / 3600.0)"


class MSSQLConnector(BaseConnector):
    """SQL Server 2016+ and Azure SQL Database."""

    name: ClassVar[str] = "mssql"
    extra: ClassVar[str] = "mssql"
    driver_module: ClassVar[str] = "pyodbc"
    dialect_class: ClassVar[type[Dialect]] = TSQLDialect
    default_port: ClassVar[int] = 1433

    def build_url(self) -> str | URL:
        if self.config.dsn:
            return self.config.dsn
        query: dict[str, str] = {
            "driver": resolve_odbc_driver(self.config.driver),
            **self.config.query_params,
        }
        # Driver 18 defaults Encrypt=yes and then rejects the self-signed certs
        # that most on-prem instances use, so be explicit about both settings.
        query.setdefault("Encrypt", "yes")
        query.setdefault("TrustServerCertificate", "yes")
        query.setdefault("timeout", str(self.config.connect_timeout))

        return URL.create(
            drivername="mssql+pyodbc",
            username=self.config.username,
            password=self.config.password,
            host=self.config.host,
            port=self.config.port or self.default_port,
            database=self.config.database,
            query=query,
        )

    def build_connect_args(self) -> dict[str, Any]:
        args = super().build_connect_args()
        args.setdefault("timeout", self.config.query_timeout)
        return args

    def session_setup_statements(self) -> Sequence[str]:
        # READ UNCOMMITTED keeps profiling queries from taking shared locks on
        # tables that OLTP writers are actively using.
        return ("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED",)

    def server_version(self) -> str | None:
        try:
            return str(self.scalar("SELECT @@VERSION"))
        except Exception:
            return None
