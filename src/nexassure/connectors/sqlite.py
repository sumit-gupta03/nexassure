# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""SQLite connector.

Ships with Python, so it is the zero-install default for the NexAssure metastore
and a convenient target for the tutorial in the README.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from sqlalchemy.engine import URL

from .base import BaseConnector, Dialect


class SQLiteDialect(Dialect):
    supports_stddev = False  # No STDDEV without the (optional) math extension.
    supports_percentile = False
    supports_regexp = False  # REGEXP needs a user-defined function to be registered.
    supports_tablesample = False
    limit_style = "limit"

    def stddev(self, expr: str) -> str:
        # Expressed from raw moments so profiling still returns a spread value.
        return f"SQRT(AVG({expr} * {expr}) - AVG({expr}) * AVG({expr}))"

    def regexp_match(self, expr: str, pattern_literal: str) -> str:
        return f"({expr} GLOB {pattern_literal})"

    def cast_double(self, expr: str) -> str:
        return f"CAST({expr} AS REAL)"

    def cast_varchar(self, expr: str) -> str:
        return f"CAST({expr} AS TEXT)"

    def hours_between(self, later: str, earlier: str) -> str:
        return f"((JULIANDAY({later}) - JULIANDAY({earlier})) * 24.0)"

    def current_timestamp(self) -> str:
        return "CURRENT_TIMESTAMP"


class SQLiteConnector(BaseConnector):
    """SQLite 3, file-backed or in-memory."""

    name: ClassVar[str] = "sqlite"
    extra: ClassVar[str] = ""
    driver_module: ClassVar[str | None] = None
    dialect_class: ClassVar[type[Dialect]] = SQLiteDialect
    serialized_access: ClassVar[bool] = True

    def build_url(self) -> str | URL:
        if self.config.dsn:
            return self.config.dsn
        path = self.config.database or ":memory:"
        return f"sqlite:///{path}"

    def connect(self) -> BaseConnector:
        if self._engine is not None:
            return self
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        in_memory = (self.config.database or ":memory:") == ":memory:"
        kwargs: dict = {"future": True, "connect_args": self.build_connect_args()}
        if in_memory:
            # Without StaticPool every checkout gets a fresh, empty database.
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {**kwargs["connect_args"], "check_same_thread": False}
        self._engine = create_engine(self.build_url(), **kwargs)
        return self

    def session_setup_statements(self) -> Sequence[str]:
        return ()

    @property
    def catalog_name(self) -> str | None:
        """Always None: ``database`` is a file path here, not a SQL catalog."""
        return None

    def server_version(self) -> str | None:
        try:
            return str(self.scalar("SELECT sqlite_version()"))
        except Exception:
            return None
