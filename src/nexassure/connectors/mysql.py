# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""MySQL / MariaDB connector (PyMySQL)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy.engine import URL

from .base import BaseConnector, Dialect


class MySQLDialect(Dialect):
    quote_char = ("`", "`")
    supports_percentile = False  # No PERCENTILE_CONT before MySQL 8.0.2 window functions.
    limit_style = "limit"

    def quote(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    def stddev(self, expr: str) -> str:
        return f"STDDEV_SAMP({expr})"

    def regexp_match(self, expr: str, pattern_literal: str) -> str:
        return f"({expr} REGEXP {pattern_literal})"

    def cast_double(self, expr: str) -> str:
        return f"CAST({expr} AS DOUBLE)"

    def cast_varchar(self, expr: str) -> str:
        return f"CAST({expr} AS CHAR(4000))"

    def hours_between(self, later: str, earlier: str) -> str:
        return f"(TIMESTAMPDIFF(SECOND, {earlier}, {later}) / 3600.0)"


class MySQLConnector(BaseConnector):
    """MySQL 5.7+ and MariaDB."""

    name: ClassVar[str] = "mysql"
    extra: ClassVar[str] = "mysql"
    driver_module: ClassVar[str] = "pymysql"
    dialect_class: ClassVar[type[Dialect]] = MySQLDialect
    default_port: ClassVar[int] = 3306

    def build_url(self) -> str | URL:
        if self.config.dsn:
            return self.config.dsn
        return URL.create(
            drivername="mysql+pymysql",
            username=self.config.username,
            password=self.config.password,
            host=self.config.host or "localhost",
            port=self.config.port or self.default_port,
            database=self.config.database or self.config.db_schema,
            query=dict(self.config.query_params),
        )

    def build_connect_args(self) -> dict[str, Any]:
        args = super().build_connect_args()
        args.setdefault("connect_timeout", self.config.connect_timeout)
        args.setdefault("read_timeout", self.config.query_timeout)
        return args

    def session_setup_statements(self) -> Sequence[str]:
        return (f"SET SESSION max_execution_time = {self.config.query_timeout * 1000}",)

    def server_version(self) -> str | None:
        try:
            return str(self.scalar("SELECT VERSION()"))
        except Exception:
            return None
