# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""PostgreSQL connector (psycopg 3)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy.engine import URL

from .base import BaseConnector, Dialect


class PostgresDialect(Dialect):
    supports_approx_count_distinct = False
    limit_style = "limit"

    def regexp_match(self, expr: str, pattern_literal: str) -> str:
        # ``~`` is the Postgres POSIX regex operator and is far cheaper than
        # REGEXP_LIKE, which only arrived in PG 15.
        return f"({expr} ~ {pattern_literal})"

    def cast_double(self, expr: str) -> str:
        return f"CAST({expr} AS DOUBLE PRECISION)"

    def hours_between(self, later: str, earlier: str) -> str:
        return f"(EXTRACT(EPOCH FROM ({later} - {earlier})) / 3600.0)"


class PostgresConnector(BaseConnector):
    """Connects to PostgreSQL, and to anything that speaks its wire protocol."""

    name: ClassVar[str] = "postgres"
    extra: ClassVar[str] = "postgres"
    driver_module: ClassVar[str] = "psycopg"
    dialect_class: ClassVar[type[Dialect]] = PostgresDialect
    default_port: ClassVar[int] = 5432

    def build_url(self) -> str | URL:
        if self.config.dsn:
            return self.config.dsn
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.config.username,
            password=self.config.password,
            host=self.config.host or "localhost",
            port=self.config.port or self.default_port,
            database=self.config.database,
            query=dict(self.config.query_params),
        )

    def build_connect_args(self) -> dict[str, Any]:
        args = super().build_connect_args()
        args.setdefault("connect_timeout", self.config.connect_timeout)
        if self.config.db_schema:
            # search_path keeps unqualified table names resolving to the
            # configured schema without rewriting every generated query.
            options = args.get("options", "")
            args["options"] = f"{options} -c search_path={self.config.db_schema}".strip()
        return args

    def session_setup_statements(self) -> Sequence[str]:
        return (f"SET statement_timeout = {self.config.query_timeout * 1000}",)

    def server_version(self) -> str | None:
        try:
            return str(self.scalar("SHOW server_version"))
        except Exception:
            return None
