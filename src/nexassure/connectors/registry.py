# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Connector registry.

Built-in connectors are registered lazily, so importing NexAssure never imports a
warehouse driver you have not installed. Third parties add engines by shipping
an ``nexassure.connectors`` entry point:

.. code-block:: toml

    [project.entry-points."nexassure.connectors"]
    clickhouse = "nexassure_clickhouse:ClickHouseConnector"
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable

from ..core.models import ConnectionConfig
from ..exceptions import UnknownConnector
from ..logging_conf import get_logger
from .base import BaseConnector

log = get_logger(__name__)

#: Registry key to ``module:ClassName``. Aliases point at the same class.
_BUILTIN: dict[str, str] = {
    "postgres": "nexassure.connectors.postgres:PostgresConnector",
    "postgresql": "nexassure.connectors.postgres:PostgresConnector",
    "pg": "nexassure.connectors.postgres:PostgresConnector",
    "redshift": "nexassure.connectors.redshift:RedshiftConnector",
    "snowflake": "nexassure.connectors.snowflake:SnowflakeConnector",
    "mssql": "nexassure.connectors.mssql:MSSQLConnector",
    "sqlserver": "nexassure.connectors.mssql:MSSQLConnector",
    "azuresql": "nexassure.connectors.mssql:MSSQLConnector",
    "synapse": "nexassure.connectors.synapse:SynapseConnector",
    "oracle": "nexassure.connectors.oracle:OracleConnector",
    "mysql": "nexassure.connectors.mysql:MySQLConnector",
    "mariadb": "nexassure.connectors.mysql:MySQLConnector",
    "duckdb": "nexassure.connectors.duckdb:DuckDBConnector",
    "sqlite": "nexassure.connectors.sqlite:SQLiteConnector",
}

_CACHE: dict[str, type[BaseConnector]] = {}
_PLUGINS_LOADED = False


def _load_entry_points() -> None:
    """Merge third-party connectors from the ``nexassure.connectors`` entry point group."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    _PLUGINS_LOADED = True
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="nexassure.connectors"):
            if ep.name not in _BUILTIN:
                _BUILTIN[ep.name] = ep.value
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("Could not scan connector entry points: %s", exc)


def register(name: str, connector: type[BaseConnector] | str) -> None:
    """Register a connector under ``name``.

    Accepts either the class itself or a lazy ``module:ClassName`` string.
    """
    key = name.strip().lower()
    if isinstance(connector, str):
        _BUILTIN[key] = connector
        _CACHE.pop(key, None)
    else:
        _CACHE[key] = connector
        _BUILTIN.setdefault(key, f"{connector.__module__}:{connector.__qualname__}")


def available() -> list[str]:
    """Every registered connector id, aliases included."""
    _load_entry_points()
    return sorted(set(_BUILTIN) | set(_CACHE))


def get_connector_class(type_name: str) -> type[BaseConnector]:
    """Resolve a connector id to its class, importing the module on first use."""
    _load_entry_points()
    key = (type_name or "").strip().lower()
    if key in _CACHE:
        return _CACHE[key]
    if key not in _BUILTIN:
        raise UnknownConnector(
            f"Unknown connector {type_name!r}. Available: {', '.join(available())}",
            requested=type_name,
            available=available(),
        )

    target = _BUILTIN[key]
    module_name, _, class_name = target.partition(":")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    _CACHE[key] = cls
    return cls


def create_connector(config: ConnectionConfig) -> BaseConnector:
    """Instantiate (but do not connect) the connector for a config."""
    return get_connector_class(config.type)(config)


def open_connection(config: ConnectionConfig) -> BaseConnector:
    """Instantiate and connect in one step."""
    connector = create_connector(config)
    connector.connect()
    return connector


def describe_connectors() -> list[dict[str, object]]:
    """Report each connector and whether its driver is importable.

    Powers ``nexassure connectors`` and the ``list_connectors`` MCP tool, so users
    can see at a glance which extras they still need to install.
    """
    _load_entry_points()
    seen: dict[str, dict[str, object]] = {}
    for key in sorted(_BUILTIN):
        try:
            cls = get_connector_class(key)
        except Exception as exc:
            seen[key] = {"id": key, "installed": False, "error": str(exc)}
            continue

        installed = True
        if cls.driver_module:
            try:
                importlib.import_module(cls.driver_module)
            except ImportError:
                installed = False

        seen[key] = {
            "id": key,
            "connector": cls.name,
            "canonical": key == cls.name,
            "driver_module": cls.driver_module,
            "extra": cls.extra or None,
            "installed": installed,
            "install_hint": None if installed else f"pip install 'nexassure[{cls.extra or key}]'",
            "default_port": cls.default_port,
        }
    return list(seen.values())


def iter_connector_classes() -> Iterable[type[BaseConnector]]:
    """Yield each distinct connector class exactly once (skipping aliases)."""
    _load_entry_points()
    emitted: set[str] = set()
    for key in sorted(_BUILTIN):
        try:
            cls = get_connector_class(key)
        except Exception:
            continue
        marker = f"{cls.__module__}.{cls.__qualname__}"
        if marker not in emitted:
            emitted.add(marker)
            yield cls
