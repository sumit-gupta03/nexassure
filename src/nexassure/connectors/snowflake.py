# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Snowflake connector.

Supports password auth, key-pair auth (``private_key_path``) and external
browser / SSO auth (``authenticator``). Unquoted identifiers fold to UPPER CASE
in Snowflake, which the dialect records so that introspection results and
user-written column names line up.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy.engine import URL

from ..exceptions import ConfigError
from .base import BaseConnector, Dialect


class SnowflakeDialect(Dialect):
    folds_upper = True
    supports_approx_count_distinct = True
    supports_tablesample = True

    def regexp_match(self, expr: str, pattern_literal: str) -> str:
        # Snowflake REGEXP_LIKE anchors the whole string implicitly, so a
        # partial-match pattern needs explicit wildcards on both ends.
        return f"REGEXP_LIKE({expr}, {pattern_literal})"

    def approx_count_distinct(self, expr: str) -> str:
        return f"APPROX_COUNT_DISTINCT({expr})"

    def hours_between(self, later: str, earlier: str) -> str:
        return f"(TIMESTAMPDIFF(second, {earlier}, {later}) / 3600.0)"

    def cast_varchar(self, expr: str) -> str:
        return f"CAST({expr} AS VARCHAR)"

    def length(self, expr: str) -> str:
        return f"LENGTH({self.cast_varchar(expr)})"


class SnowflakeConnector(BaseConnector):
    """Snowflake Data Cloud."""

    name: ClassVar[str] = "snowflake"
    extra: ClassVar[str] = "snowflake"
    driver_module: ClassVar[str] = "snowflake.sqlalchemy"
    dialect_class: ClassVar[type[Dialect]] = SnowflakeDialect

    def build_url(self) -> str | URL:
        if self.config.dsn:
            return self.config.dsn
        if not self.config.account:
            raise ConfigError(
                f"Connection {self.config.name!r} needs an 'account' "
                "(for example xy12345.eu-west-1)",
                connection=self.config.name,
            )
        query: dict[str, str] = dict(self.config.query_params)
        if self.config.warehouse:
            query["warehouse"] = self.config.warehouse
        if self.config.role:
            query["role"] = self.config.role
        if self.config.db_schema:
            query["schema"] = self.config.db_schema
        if self.config.authenticator:
            query["authenticator"] = self.config.authenticator

        return URL.create(
            drivername="snowflake",
            username=self.config.username,
            password=self.config.password if not self.config.private_key_path else None,
            host=self.config.account,
            database=self.config.database,
            query=query,
        )

    def build_connect_args(self) -> dict[str, Any]:
        args = super().build_connect_args()
        args.setdefault("login_timeout", self.config.connect_timeout)
        args.setdefault("network_timeout", self.config.query_timeout)
        args.setdefault("client_session_keep_alive", True)
        if self.config.private_key_path:
            args["private_key"] = self._load_private_key()
        return args

    def _load_private_key(self) -> bytes:
        """Read and normalise a PEM key into the DER bytes the driver wants.

        The passphrase, when the key has one, comes from ``SNOWFLAKE_PRIVATE_KEY_PASSPHRASE``
        so it never has to appear in a config file.
        """
        import os

        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        path = Path(self.config.private_key_path or "")
        if not path.is_file():
            raise ConfigError(f"Private key not found at {path}", connection=self.config.name)

        passphrase = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        key = serialization.load_pem_private_key(
            path.read_bytes(),
            password=passphrase.encode() if passphrase else None,
            backend=default_backend(),
        )
        return key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def session_setup_statements(self) -> Sequence[str]:
        statements = [
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {self.config.query_timeout}"
        ]
        if self.config.warehouse:
            statements.append(f"USE WAREHOUSE {self.dialect.quote(self.config.warehouse)}")
        return statements

    def ping_statement(self) -> str:
        return "SELECT CURRENT_VERSION()"

    def server_version(self) -> str | None:
        try:
            return str(self.scalar("SELECT CURRENT_VERSION()"))
        except Exception:
            return None
