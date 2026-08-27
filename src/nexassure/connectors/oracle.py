# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Oracle Database connector (python-oracledb, thin mode by default).

Oracle is the most divergent of the supported engines:

* no ``LIMIT`` before 12c, so ``FETCH FIRST n ROWS ONLY`` is used throughout
* every ``SELECT`` needs a ``FROM``, hence ``FROM DUAL``
* the empty string and ``NULL`` are the same value, which changes what a
  "blank string" check can even mean
* unquoted identifiers fold to UPPER CASE
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy.engine import URL

from ..exceptions import ConfigError
from .base import BaseConnector, Dialect


class OracleDialect(Dialect):
    folds_upper = True
    supports_approx_count_distinct = True
    supports_tablesample = True
    limit_style = "fetch_first"

    def stddev(self, expr: str) -> str:
        return f"STDDEV({expr})"

    def approx_count_distinct(self, expr: str) -> str:
        return f"APPROX_COUNT_DISTINCT({expr})"

    def regexp_match(self, expr: str, pattern_literal: str) -> str:
        return f"REGEXP_LIKE({expr}, {pattern_literal})"

    def cast_double(self, expr: str) -> str:
        return f"CAST({expr} AS BINARY_DOUBLE)"

    def cast_varchar(self, expr: str) -> str:
        return f"CAST({expr} AS VARCHAR2(4000))"

    def hours_between(self, later: str, earlier: str) -> str:
        # Subtracting two DATEs yields days as a NUMBER; multiply to get hours.
        return f"(({later} - {earlier}) * 24)"

    def current_timestamp(self) -> str:
        return "SYSTIMESTAMP"


class OracleConnector(BaseConnector):
    """Oracle Database 12c and later, including Autonomous Database."""

    name: ClassVar[str] = "oracle"
    extra: ClassVar[str] = "oracle"
    driver_module: ClassVar[str] = "oracledb"
    dialect_class: ClassVar[type[Dialect]] = OracleDialect
    default_port: ClassVar[int] = 1521

    def build_url(self) -> str | URL:
        if self.config.dsn:
            return self.config.dsn
        service = self.config.service_name or self.config.database
        if not service:
            raise ConfigError(
                f"Connection {self.config.name!r} needs a 'service_name' (or 'database')",
                connection=self.config.name,
            )
        query = {"service_name": service, **self.config.query_params}
        return URL.create(
            drivername="oracle+oracledb",
            username=self.config.username,
            password=self.config.password,
            host=self.config.host,
            port=self.config.port or self.default_port,
            query=query,
        )

    def build_connect_args(self) -> dict[str, Any]:
        args = super().build_connect_args()
        args.setdefault("tcp_connect_timeout", self.config.connect_timeout)
        return args

    def session_setup_statements(self) -> Sequence[str]:
        statements = []
        if self.config.db_schema:
            # Oracle calls a schema a user; setting it lets unqualified names resolve.
            statements.append(
                f"ALTER SESSION SET CURRENT_SCHEMA = {self.dialect.quote(self.config.db_schema)}"
            )
        return statements

    def ping_statement(self) -> str:
        return "SELECT 1 FROM DUAL"

    def list_schemas(self) -> list[str]:
        """List schemas that actually hold objects.

        ``ALL_USERS`` includes dozens of Oracle-internal accounts; filtering to
        owners present in ``ALL_TABLES`` keeps discovery output usable.
        """
        result = self.execute("SELECT DISTINCT owner FROM all_tables ORDER BY owner", max_rows=None)
        return [row[0] for row in result.rows]

    def server_version(self) -> str | None:
        try:
            return str(self.scalar("SELECT banner FROM v$version FETCH FIRST 1 ROWS ONLY"))
        except Exception:
            try:
                return str(
                    self.scalar(
                        "SELECT version FROM product_component_version FETCH FIRST 1 ROWS ONLY"
                    )
                )
            except Exception:
                return None
