# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""The NexAssure facade.

:class:`NexAssure` ties the pieces together - config, connectors, metastore,
profiler, runner, notifications - behind one object. The CLI, the REST API and
the MCP server are all thin shells over this class, which is why they cannot
drift apart in behaviour.

Typical use::

    from nexassure import NexAssure

    with NexAssure() as na:
        na.test_connection("prod_snowflake")
        profile = na.profile("prod_snowflake", "ANALYTICS.PUBLIC.ORDERS")
        run = na.run_suite("orders_quality")
        print(run.summary)
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .checks.base import CheckContext, build_check
from .config import ProjectConfig, connection_from_dsn, load_config
from .connectors.base import BaseConnector, QueryResult
from .connectors.registry import create_connector
from .core.engine import ProgressCallback, SuiteRunner
from .core.models import (
    CheckResult,
    CheckSpec,
    ConnectionConfig,
    RunResult,
    Suite,
    TableProfile,
    TableRef,
)
from .exceptions import ConfigError, NexAssureError, SuiteError
from .logging_conf import get_logger
from .metastore.repository import Metastore, bootstrap_on_connect
from .profiling.inference import InferenceOptions, suggest_suite
from .profiling.profiler import ProfileOptions, Profiler
from .suites.loader import load_suites, validate_suite, validate_suites

log = get_logger(__name__)


class NexAssure:
    """Entry point for programmatic use."""

    def __init__(
        self,
        config: ProjectConfig | str | Path | None = None,
        *,
        metastore_url: str | None = None,
        environment: str | None = None,
    ) -> None:
        if isinstance(config, ProjectConfig):
            self.config = config
        else:
            self.config = load_config(config)

        self.environment = environment or self.config.defaults.environment
        self._metastore_url = metastore_url or self.config.metastore_url()
        self._metastore: Metastore | None = None
        self._connectors: dict[str, BaseConnector] = {}
        self._bootstrapped: set[str] = set()
        self._suites: list[Suite] | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def __enter__(self) -> NexAssure:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Dispose of every open connector and the metastore engine."""
        for connector in self._connectors.values():
            try:
                connector.close()
            except Exception as exc:  # pragma: no cover - cleanup must not raise
                log.debug("Error closing connector: %s", exc)
        self._connectors.clear()
        self._bootstrapped.clear()
        if self._metastore is not None:
            self._metastore.close()
            self._metastore = None

    @property
    def metastore(self) -> Metastore | None:
        """The metastore, or ``None`` when history recording is disabled."""
        if not self.config.metastore.enabled:
            return None
        if self._metastore is None:
            self._metastore = Metastore(self._metastore_url, strict=self.config.metastore.strict)
        return self._metastore

    # -- connections -------------------------------------------------------- #

    def register_connection(self, config: ConnectionConfig) -> None:
        """Add a connection at runtime, without editing ``nexassure.yml``."""
        self.config.connections = [c for c in self.config.connections if c.name != config.name] + [
            config
        ]
        cached = self._connectors.pop(config.name, None)
        if cached is not None:
            cached.close()
        self._bootstrapped.discard(config.name)

    def register_dsn(self, dsn: str, name: str = "adhoc") -> ConnectionConfig:
        """Register a connection from a bare SQLAlchemy URL and return it."""
        config = connection_from_dsn(dsn, name)
        self.register_connection(config)
        return config

    def connect(self, name: str, *, discover: bool | None = None) -> BaseConnector:
        """Open (or reuse) a connection.

        On the first connect for a name, the metastore is created if missing,
        the connection is registered, and - unless discovery is turned off -
        the catalog is walked so ``nexassure_datasets`` and ``nexassure_columns`` are
        populated. This is what makes the metadata tables show up as soon as a
        driver is wired in.
        """
        connector = self._connectors.get(name)
        if connector is None:
            connector = create_connector(self.config.connection(name))
            connector.connect()
            self._connectors[name] = connector

        if name not in self._bootstrapped:
            self._bootstrapped.add(name)
            self._bootstrap(connector, discover)
        return connector

    def _bootstrap(self, connector: BaseConnector, discover: bool | None) -> None:
        store = self.metastore
        if store is None:
            return
        should_discover = self.config.metastore.auto_discover if discover is None else discover
        try:
            bootstrap_on_connect(
                store,
                connector,
                discover=should_discover,
                max_tables=self.config.metastore.discovery_limit,
            )
        except Exception as exc:
            # Cataloguing is a convenience; never let it block the actual work.
            log.warning("Metastore bootstrap for %r failed: %s", connector.config.name, exc)

    def test_connection(self, name: str) -> dict[str, Any]:
        """Verify credentials and report latency without cataloguing anything."""
        try:
            config = self.config.connection(name)
        except ConfigError as exc:
            return {"ok": False, "connection": name, "error": exc.message}

        connector = create_connector(config)
        try:
            outcome = connector.test_connection()
            store = self.metastore
            if store is not None:
                store.register_connection(
                    config,
                    server_version=outcome.get("server_version"),
                    status="connected" if outcome["ok"] else "failed",
                    error=outcome.get("error"),
                )
            return outcome
        finally:
            connector.close()

    def test_all_connections(self) -> list[dict[str, Any]]:
        return [self.test_connection(c.name) for c in self.config.connections]

    def list_connections(self) -> list[dict[str, Any]]:
        return [c.safe_summary for c in self.config.connections]

    # -- catalog ------------------------------------------------------------ #

    def list_schemas(self, connection: str) -> list[str]:
        return self.connect(connection, discover=False).list_schemas()

    def list_tables(self, connection: str, schema: str | None = None) -> list[str]:
        connector = self.connect(connection, discover=False)
        return [ref.fqn for ref in connector.list_tables(schema=schema)]

    def describe_table(self, connection: str, table: str) -> dict[str, Any]:
        """Introspect a table and record it in the metastore."""
        connector = self.connect(connection, discover=False)
        dataset = connector.describe_table(TableRef.parse(table))
        store = self.metastore
        if store is not None:
            store.upsert_dataset(connection, dataset)
        return dataset.model_dump(mode="json", by_alias=True)

    def discover(
        self, connection: str, schemas: Iterable[str] | None = None, max_tables: int | None = None
    ) -> dict[str, Any]:
        """Walk the catalog and populate the metastore. Returns what was found."""
        connector = self.connect(connection, discover=False)
        store = self.metastore
        if store is None:
            raise NexAssureError("Discovery needs the metastore, which is disabled in this project")
        return bootstrap_on_connect(
            store,
            connector,
            discover=True,
            schemas=schemas,
            max_tables=max_tables or self.config.metastore.discovery_limit,
        )

    def query(
        self, connection: str, sql: str, params: dict[str, Any] | None = None, max_rows: int = 1000
    ) -> QueryResult:
        """Run an ad-hoc read-only query.

        Subject to the same read-only guard as checks, so this is safe to expose
        over the REST API and to an MCP client.
        """
        return self.connect(connection, discover=False).execute(sql, params, max_rows=max_rows)

    # -- profiling ---------------------------------------------------------- #

    def profile(
        self,
        connection: str,
        table: str,
        options: ProfileOptions | None = None,
        *,
        record: bool = True,
    ) -> TableProfile:
        """Profile one table, optionally recording the snapshot."""
        connector = self.connect(connection, discover=False)
        profile = Profiler(connector, options).profile_table(TableRef.parse(table))
        store = self.metastore
        if record and store is not None:
            store.record_profile(profile)
        return profile

    def profile_schema(
        self,
        connection: str,
        schema: str | None = None,
        limit: int = 50,
        options: ProfileOptions | None = None,
        *,
        record: bool = True,
    ) -> list[TableProfile]:
        """Profile every table in a schema."""
        connector = self.connect(connection, discover=False)
        profiles = Profiler(connector, options).profile_schema(schema, limit)
        store = self.metastore
        if record and store is not None:
            for profile in profiles:
                store.record_profile(profile)
        return profiles

    def suggest(
        self,
        connection: str,
        tables: list[str] | None = None,
        schema: str | None = None,
        *,
        suite_name: str = "suggested",
        limit: int = 20,
        profile_options: ProfileOptions | None = None,
        inference_options: InferenceOptions | None = None,
    ) -> Suite:
        """Profile tables and propose a starter suite from what is there."""
        if tables:
            profiles = [
                self.profile(connection, table, profile_options, record=False) for table in tables
            ]
        else:
            profiles = self.profile_schema(connection, schema, limit, profile_options, record=False)
        if not profiles:
            raise NexAssureError(
                f"No tables found to profile on {connection!r}"
                + (f" in schema {schema!r}" if schema else "")
            )
        return suggest_suite(profiles, connection, suite_name, inference_options)

    # -- suites ------------------------------------------------------------- #

    def suites(self, reload: bool = False) -> list[Suite]:
        """Every suite the project config points at, loaded once and cached."""
        if self._suites is None or reload:
            paths = self.config.suite_paths()
            self._suites = load_suites(paths) if paths else []
            log.debug("Loaded %s suite(s)", len(self._suites))
        return self._suites

    def suite(self, name: str) -> Suite:
        for suite in self.suites():
            if suite.name == name:
                return suite
        known = ", ".join(s.name for s in self.suites()) or "none found"
        raise SuiteError(
            f"Unknown suite {name!r}. Loaded suites: {known}",
            requested=name,
            available=[s.name for s in self.suites()],
        )

    def validate(self) -> dict[str, list[str]]:
        """Lint every suite offline. Returns ``{suite: problems}`` for failures only."""
        return validate_suites(self.suites())

    def sync_suites(self) -> int:
        """Mirror every suite definition into the metastore check registry."""
        store = self.metastore
        if store is None:
            return 0
        return sum(store.sync_suite(suite) for suite in self.suites())

    # -- running ------------------------------------------------------------ #

    def run_suite(
        self,
        suite: str | Suite,
        *,
        select: list[str] | None = None,
        tags: list[str] | None = None,
        datasets: list[str] | None = None,
        max_parallel: int | None = None,
        fail_fast: bool | None = None,
        dry_run: bool = False,
        triggered_by: str = "manual",
        record: bool = True,
        notify: bool = True,
        on_result: ProgressCallback | None = None,
    ) -> RunResult:
        """Execute one suite end to end.

        Validation runs first and hard-fails the run: executing a suite with a
        typo in a check type would otherwise produce a wall of ``ERRORED``
        results that hide the single real problem.
        """
        target = self.suite(suite) if isinstance(suite, str) else suite

        problems = validate_suite(target)
        if problems:
            raise SuiteError(
                f"Suite {target.name!r} has {len(problems)} validation problem(s):\n  - "
                + "\n  - ".join(problems),
                suite=target.name,
                problems=problems,
            )

        connector = self.connect(target.connection)
        runner = SuiteRunner(
            connector,
            max_parallel=max_parallel or self.config.defaults.max_parallel,
            fail_fast=fail_fast if fail_fast is not None else self.config.defaults.fail_fast,
            environment=self.environment,
            on_result=on_result,
        )
        run = runner.run(
            target,
            select=select,
            tags=tags,
            datasets=datasets,
            triggered_by=triggered_by,
            dry_run=dry_run,
        )

        store = self.metastore
        if record and store is not None and not dry_run:
            store.sync_suite(target)
            store.record_run(run)
        if notify and not dry_run:
            self._notify(run)
        return run

    def run_all(
        self,
        *,
        tags: list[str] | None = None,
        suites: list[str] | None = None,
        **kwargs: Any,
    ) -> list[RunResult]:
        """Run every loaded suite, or the subset named by ``suites``."""
        targets = self.suites()
        if suites:
            wanted = set(suites)
            targets = [s for s in targets if s.name in wanted]
        if tags:
            wanted = set(tags)
            targets = [s for s in targets if wanted & set(s.tags)] or targets

        results = []
        for target in targets:
            try:
                results.append(self.run_suite(target, tags=tags, **kwargs))
            except NexAssureError as exc:
                log.error("Suite %r could not run: %s", target.name, exc)
                failed = RunResult(suite_name=target.name, connection_name=target.connection)
                failed.error = str(exc)
                failed.status = failed.status.ERRORED
                results.append(failed)
        return results

    def run_check(self, connection: str, spec: CheckSpec) -> CheckResult:
        """Execute a single ad-hoc check without a suite file.

        Used by the MCP ``run_check`` tool, so an agent can test a hypothesis
        about the data without writing anything to disk.
        """
        connector = self.connect(connection, discover=False)
        ctx = CheckContext(connector=connector, suite_name="adhoc")
        return build_check(spec).run(ctx)

    # -- notifications ------------------------------------------------------ #

    def _notify(self, run: RunResult) -> None:
        settings = self.config.notifications
        if settings.notify_on == "never":
            return
        if settings.notify_on == "failure" and run.exit_code == 0:
            return
        if not (settings.slack_webhook or settings.webhook_url):
            return

        try:
            from .notifications.dispatch import dispatch

            dispatch(run, settings)
        except Exception as exc:
            # A broken webhook must not change the outcome of a test run.
            log.warning("Notification failed: %s", exc)

    # -- history ------------------------------------------------------------ #

    def history(self, suite: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        store = self.metastore
        return store.list_runs(suite, limit) if store else []

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        store = self.metastore
        return store.get_run(run_id) if store else None

    def summary(self, since_hours: int = 24) -> dict[str, Any]:
        store = self.metastore
        return store.summary(since_hours) if store else {}
