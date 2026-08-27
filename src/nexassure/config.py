# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Project configuration.

A project is described by a single ``nexassure.yml`` at the repo root:

.. code-block:: yaml

    version: 1
    project: analytics-warehouse
    metastore:
      url: postgresql+psycopg://nexassure@metastore/nexassure
    connections:
      - name: prod_snowflake
        type: snowflake
        account: xy12345.eu-west-1
        username: ${env:SNOWFLAKE_USER}
        password: ${env:SNOWFLAKE_PASSWORD}
        warehouse: ANALYTICS_WH
        database: PROD
        schema: PUBLIC
    suites:
      - suites/*.yml

Credentials are written as ``${env:VAR}`` references and resolved at load time,
so the file is safe to commit.

Discovery walks up from the working directory, the way ``git`` finds its root,
so ``nexassure run`` works from any subdirectory of the project.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .core.models import ConnectionConfig, resolve_env
from .exceptions import ConfigError
from .logging_conf import get_logger

log = get_logger(__name__)

#: Filenames accepted as a project file, in priority order.
CONFIG_FILENAMES = ("nexassure.yml", "nexassure.yaml", ".nexassure.yml", ".nexassure.yaml")


class MetastoreConfig(BaseModel):
    """Where run history is written."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(None, description="SQLAlchemy URL; defaults to a local SQLite file")
    enabled: bool = True
    strict: bool = Field(
        False, description="Fail the run when the metastore is unreachable, instead of warning"
    )
    #: Catalog tables into nexassure_datasets/nexassure_columns whenever a connection opens.
    auto_discover: bool = True
    #: Cap on tables catalogued per connect, so huge warehouses stay usable.
    discovery_limit: int = 200
    retention_days: int | None = Field(
        None, description="Purge runs and profiles older than this on 'nexassure metastore purge'"
    )

    @field_validator("url", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        return resolve_env(value) if isinstance(value, str) else value


class NotificationConfig(BaseModel):
    """Where failures get announced."""

    model_config = ConfigDict(extra="forbid")

    slack_webhook: str | None = None
    webhook_url: str | None = None
    notify_on: str = Field("failure", description="always | failure | never")
    include_passed: bool = False

    @field_validator("slack_webhook", "webhook_url", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        return resolve_env(value) if isinstance(value, str) else value


class RunDefaults(BaseModel):
    """Defaults applied to every run unless overridden on the CLI."""

    model_config = ConfigDict(extra="forbid")

    max_parallel: int = Field(8, ge=1, le=64)
    fail_fast: bool = False
    environment: str | None = None
    sample_limit: int = 10


class ProjectConfig(BaseModel):
    """The parsed ``nexassure.yml``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: int = 1
    project: str = "nexassure"
    description: str | None = None

    connections: list[ConnectionConfig] = Field(default_factory=list)
    #: Glob patterns, relative to the project root, that locate suite files.
    suites: list[str] = Field(default_factory=lambda: ["suites/**/*.yml", "suites/**/*.yaml"])

    metastore: MetastoreConfig = Field(default_factory=MetastoreConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    defaults: RunDefaults = Field(default_factory=RunDefaults)

    #: Absolute path of the file this came from; ``None`` when built in memory.
    root: Path | None = Field(None, exclude=True)

    @property
    def project_dir(self) -> Path:
        return self.root.parent if self.root else Path.cwd()

    def connection(self, name: str) -> ConnectionConfig:
        """Look up a connection by name, with a helpful error when it is missing."""
        for candidate in self.connections:
            if candidate.name == name:
                return candidate
        known = ", ".join(c.name for c in self.connections) or "none configured"
        raise ConfigError(
            f"Unknown connection {name!r}. Configured connections: {known}",
            requested=name,
            available=[c.name for c in self.connections],
        )

    def suite_paths(self) -> list[Path]:
        """Expand the ``suites`` globs into concrete, de-duplicated file paths."""
        base = self.project_dir
        found: list[Path] = []
        seen: set[Path] = set()
        for pattern in self.suites:
            candidate = base / pattern
            # A literal path is far more common than a glob in small projects.
            matches = [candidate] if candidate.is_file() else sorted(base.glob(pattern))
            for match in matches:
                resolved = match.resolve()
                if match.is_file() and resolved not in seen:
                    seen.add(resolved)
                    found.append(match)
        return found

    def metastore_url(self) -> str | None:
        if not self.metastore.enabled:
            return None
        return self.metastore.url


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for a project file.

    Stops at the filesystem root. ``NEXASSURE_CONFIG`` short-circuits the search.
    """
    override = os.getenv("NEXASSURE_CONFIG")
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None

    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for filename in CONFIG_FILENAMES:
            candidate = directory / filename
            if candidate.is_file():
                return candidate
    return None


def load_config(path: str | Path | None = None) -> ProjectConfig:
    """Load a project config, or return a usable empty one.

    A missing config file is not an error: ``nexassure`` still works with an
    inline DSN (``--dsn``), which is how most people take their first look.
    """
    resolved = Path(path).expanduser() if path else find_config()
    if resolved is None:
        log.debug("No nexassure.yml found; using defaults")
        return ProjectConfig()

    resolved = resolved.resolve()
    if not resolved.is_file():
        raise ConfigError(f"Config file not found: {resolved}", path=str(resolved))

    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {resolved}: {exc}", path=str(resolved)) from exc

    if not isinstance(raw, dict):
        raise ConfigError(
            f"{resolved} must contain a YAML mapping at the top level", path=str(resolved)
        )

    try:
        config = ProjectConfig(**raw)
    except KeyError as exc:
        # Raised by resolve_env for an unset ${env:VAR} reference.
        raise ConfigError(f"{resolved}: {exc.args[0]}", path=str(resolved)) from exc
    except Exception as exc:
        raise ConfigError(f"Invalid config in {resolved}: {exc}", path=str(resolved)) from exc

    config.root = resolved
    log.debug("Loaded project %r from %s", config.project, resolved)
    return config


def connection_from_dsn(dsn: str, name: str = "adhoc") -> ConnectionConfig:
    """Build a connection from a bare SQLAlchemy URL.

    Powers ``nexassure profile --dsn ...`` and the MCP ``connect`` tool, so someone
    can point NexAssure at a database without writing a config file first.
    """
    try:
        from sqlalchemy.engine import make_url

        url = make_url(dsn)
    except Exception as exc:
        raise ConfigError(f"Could not parse DSN: {exc}", dsn=dsn[:80]) from exc

    backend = url.get_backend_name().lower()
    #: SQLAlchemy backend names do not always match NexAssure connector ids.
    aliases = {"postgresql": "postgres", "mssql": "mssql", "oracle": "oracle"}

    return ConnectionConfig(
        name=name,
        type=aliases.get(backend, backend),
        dsn=dsn,
        database=url.database,
        host=url.host,
        port=url.port,
        username=url.username,
    )


def write_starter_config(directory: Path, project_name: str = "nexassure") -> Path:
    """Write a commented ``nexassure.yml`` for ``nexassure init``."""
    target = directory / "nexassure.yml"
    if target.exists():
        raise ConfigError(f"{target} already exists", path=str(target))

    target.write_text(STARTER_CONFIG.replace("__PROJECT__", project_name), encoding="utf-8")
    return target


STARTER_CONFIG = """\
# NexAssure project configuration.
# Docs: https://github.com/sumit-gupta03/nexassure/tree/main/docs
version: 1
project: __PROJECT__

# Where run history, profiles and the data catalog are stored.
# Defaults to a SQLite file in ~/.nexassure. Point it at Postgres to share
# history across a team or across CI runners.
metastore:
  # url: postgresql+psycopg://nexassure:${env:METASTORE_PASSWORD}@localhost:5432/nexassure
  auto_discover: true
  discovery_limit: 200

# Never put secrets in this file - use ${env:VAR} and keep the value in
# the environment, a .env file, or your secret manager.
connections:
  - name: local
    type: duckdb
    database: ./warehouse.duckdb

  # - name: prod_snowflake
  #   type: snowflake
  #   account: ${env:SNOWFLAKE_ACCOUNT}
  #   username: ${env:SNOWFLAKE_USER}
  #   password: ${env:SNOWFLAKE_PASSWORD}
  #   warehouse: ANALYTICS_WH
  #   database: PROD
  #   schema: PUBLIC
  #   role: DATA_QUALITY_READER

  # - name: prod_postgres
  #   type: postgres
  #   host: ${env:PGHOST}
  #   port: 5432
  #   database: analytics
  #   schema: public
  #   username: ${env:PGUSER}
  #   password: ${env:PGPASSWORD}

# Glob patterns locating suite files, relative to this file.
suites:
  - suites/**/*.yml

defaults:
  max_parallel: 8
  fail_fast: false

notifications:
  notify_on: failure
  # slack_webhook: ${env:SLACK_WEBHOOK_URL}
"""
