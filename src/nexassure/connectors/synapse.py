# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Azure Synapse Analytics connector.

Synapse speaks T-SQL over the same ODBC driver as SQL Server, but the dedicated
SQL pool is a distributed MPP engine with real restrictions:

* no ``TABLESAMPLE``
* no ``APPROX_COUNT_DISTINCT`` on dedicated pools
* ``PERCENTILE_CONT`` only in its windowed form
* Azure AD auth is the norm rather than SQL logins

Serverless SQL pools behave more like SQL Server, so the ``pool`` parameter
selects which set of limits to assume.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy.engine import URL

from .base import Dialect
from .mssql import MSSQLConnector, TSQLDialect, resolve_odbc_driver


class SynapseDialect(TSQLDialect):
    supports_tablesample = False
    supports_approx_count_distinct = False

    def percentile(self, expr: str, fraction: float) -> str:
        # Dedicated pools only accept the OVER () form, and only inside a
        # SELECT DISTINCT, so the profiler treats percentiles as best-effort.
        return f"PERCENTILE_CONT({fraction}) WITHIN GROUP (ORDER BY {expr}) OVER ()"

    def approx_count_distinct(self, expr: str) -> str:
        return f"COUNT(DISTINCT {expr})"


class SynapseConnector(MSSQLConnector):
    """Azure Synapse dedicated and serverless SQL pools."""

    name: ClassVar[str] = "synapse"
    extra: ClassVar[str] = "synapse"
    driver_module: ClassVar[str] = "pyodbc"
    dialect_class: ClassVar[type[Dialect]] = SynapseDialect
    default_port: ClassVar[int] = 1433

    def build_url(self) -> str | URL:
        if self.config.dsn:
            return self.config.dsn
        query: dict[str, str] = {
            "driver": resolve_odbc_driver(self.config.driver),
            **self.config.query_params,
        }
        # Synapse endpoints are always TLS and always present a valid cert, so
        # unlike on-prem SQL Server there is no reason to relax verification.
        query.setdefault("Encrypt", "yes")
        query.setdefault("TrustServerCertificate", "no")
        query.setdefault("timeout", str(self.config.connect_timeout))
        if self.config.authenticator:
            # e.g. ActiveDirectoryMsi, ActiveDirectoryServicePrincipal,
            # ActiveDirectoryPassword, ActiveDirectoryInteractive
            query.setdefault("Authentication", self.config.authenticator)

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
        # Synapse dedicated pools do not support MARS.
        args.pop("MARS_Connection", None)
        return args

    def session_setup_statements(self) -> Sequence[str]:
        # Dedicated pools reject SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED
        # in some configurations, and default to it anyway, so skip session setup.
        return ()

    def list_tables(self, schema: str | None = None, include_views: bool = True):
        """Read the catalog directly.

        The SQLAlchemy inspector issues several round trips per call against
        Synapse; one catalog query is markedly faster when discovering a schema
        with hundreds of tables.
        """
        from ..core.models import TableRef

        target = schema or self.config.db_schema
        kinds = "('U', 'V')" if include_views else "('U')"
        sql = f"""
            SELECT s.name AS schema_name, t.name AS table_name
            FROM sys.objects AS t
            JOIN sys.schemas AS s ON t.schema_id = s.schema_id
            WHERE t.type IN {kinds}
        """
        params: dict[str, Any] = {}
        if target:
            sql += " AND s.name = :schema_name"
            params["schema_name"] = target
        sql += " ORDER BY s.name, t.name"

        result = self.execute(sql, params, max_rows=None)
        return [
            TableRef(database=self.config.database, schema=row[0], table=row[1])
            for row in result.rows
        ]

    def server_version(self) -> str | None:
        try:
            return str(self.scalar("SELECT @@VERSION"))
        except Exception:
            return None
