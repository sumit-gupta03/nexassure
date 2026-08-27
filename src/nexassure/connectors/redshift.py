# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Amazon Redshift connector.

Redshift forked from PostgreSQL 8.0, so the syntax looks familiar but several
modern functions are missing. The dialect below encodes those gaps rather than
letting checks fail at runtime with a confusing parser error.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy.engine import URL

from .base import BaseConnector, Dialect


class RedshiftDialect(Dialect):
    # Redshift has APPROXIMATE COUNT(DISTINCT ...) via HyperLogLog, which turns
    # a full-table distinct on a billion-row fact table into a cheap sketch.
    supports_approx_count_distinct = True
    supports_percentile = True
    supports_tablesample = False
    limit_style = "limit"

    def regexp_match(self, expr: str, pattern_literal: str) -> str:
        return f"({expr} ~ {pattern_literal})"

    def percentile(self, expr: str, fraction: float) -> str:
        # Redshift requires the OVER () clause form in some contexts; the
        # WITHIN GROUP form is valid in a plain aggregate SELECT.
        return f"PERCENTILE_CONT({fraction}) WITHIN GROUP (ORDER BY {expr})"

    def approx_count_distinct(self, expr: str) -> str:
        return f"APPROXIMATE COUNT(DISTINCT {expr})"

    def hours_between(self, later: str, earlier: str) -> str:
        return f"(DATEDIFF(second, {earlier}, {later}) / 3600.0)"

    def cast_varchar(self, expr: str) -> str:
        return f"CAST({expr} AS VARCHAR(65535))"


class RedshiftConnector(BaseConnector):
    """Redshift provisioned clusters and Redshift Serverless."""

    name: ClassVar[str] = "redshift"
    extra: ClassVar[str] = "redshift"
    driver_module: ClassVar[str] = "redshift_connector"
    dialect_class: ClassVar[type[Dialect]] = RedshiftDialect
    default_port: ClassVar[int] = 5439

    def build_url(self) -> str | URL:
        if self.config.dsn:
            return self.config.dsn
        return URL.create(
            drivername="redshift+redshift_connector",
            username=self.config.username,
            password=self.config.password,
            host=self.config.host,
            port=self.config.port or self.default_port,
            database=self.config.database,
            query=dict(self.config.query_params),
        )

    def build_connect_args(self) -> dict[str, Any]:
        args = super().build_connect_args()
        args.setdefault("timeout", self.config.connect_timeout)
        if self.config.db_schema:
            args.setdefault("db_user", self.config.username)
        return args

    def session_setup_statements(self) -> Sequence[str]:
        statements = [f"SET statement_timeout TO {self.config.query_timeout * 1000}"]
        if self.config.db_schema:
            statements.append(f"SET search_path TO {self.config.db_schema}")
        return statements

    def server_version(self) -> str | None:
        try:
            return str(self.scalar("SELECT version()"))
        except Exception:
            return None
