# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Metastore read/write layer.

:class:`Metastore` owns the lifecycle (create tables on demand, upsert, query)
and is safe to construct repeatedly - table creation is idempotent and guarded
by a process-level lock so parallel check execution cannot race on DDL.

Writes are best-effort by design. A metastore outage should degrade NexAssure to
"still runs your tests, just does not record history"; it must never turn a
green pipeline red. Every write path therefore catches, logs and continues,
except when the caller explicitly asks for strict mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from ..core.models import (
    CheckSpec,
    ColumnProfile,
    ConnectionConfig,
    DatasetInfo,
    RunResult,
    Suite,
    TableProfile,
    utcnow,
)
from ..exceptions import MetastoreError
from ..logging_conf import get_logger
from .schema import (
    ALL_TABLES,
    SCHEMA_VERSION,
    checks_table,
    column_profiles_table,
    columns_table,
    connections_table,
    datasets_table,
    meta_table,
    metadata,
    profiles_table,
    results_table,
    runs_table,
    schedules_table,
)

log = get_logger(__name__)

_DDL_LOCK = threading.Lock()


def default_metastore_url() -> str:
    """Where history goes when nothing is configured.

    A SQLite file under ``~/.nexassure`` means a fresh install works with no setup;
    ``NEXASSURE_METASTORE_URL`` swaps in Postgres for shared team history.
    """
    configured = os.getenv("NEXASSURE_METASTORE_URL")
    if configured:
        return configured
    home = Path(os.getenv("NEXASSURE_HOME") or (Path.home() / ".nexassure"))
    home.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(home / 'metastore.db').as_posix()}"


def _hash_id(*parts: Any) -> str:
    """Deterministic 32-char id, so re-ingesting the same object updates in place."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:32]


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class Metastore:
    """Persistent store for connections, datasets, checks, runs and profiles."""

    def __init__(self, url: str | None = None, *, strict: bool = False, echo: bool = False) -> None:
        self.url = url or default_metastore_url()
        self.strict = strict
        self._engine: Engine | None = None
        self._echo = echo
        self._ready = False

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            try:
                self._engine = create_engine(self.url, future=True, echo=self._echo)
            except SQLAlchemyError as exc:
                raise MetastoreError(f"Invalid metastore URL {self.url!r}: {exc}") from exc
        return self._engine

    def bootstrap(self) -> bool:
        """Create the NexAssure tables if they are missing.

        Called automatically the first time anything touches the metastore -
        this is what makes the metadata tables appear as soon as you connect a
        driver, with no migration command to remember.

        Returns:
            ``True`` when the metastore is usable.
        """
        if self._ready:
            return True
        with _DDL_LOCK:
            if self._ready:
                return True
            try:
                metadata.create_all(self.engine, tables=list(ALL_TABLES), checkfirst=True)
                self._record_schema_version()
                self._ready = True
                log.debug("Metastore ready at %s", self._safe_url())
            except SQLAlchemyError as exc:
                if self.strict:
                    raise MetastoreError(f"Could not initialise metastore: {exc}") from exc
                log.warning("Metastore unavailable, history will not be recorded: %s", exc)
                return False
        return True

    def _record_schema_version(self) -> None:
        now = utcnow()
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(meta_table.c.value).where(meta_table.c.key == "schema_version")
            ).scalar()
            if existing is None:
                conn.execute(
                    meta_table.insert().values(
                        key="schema_version", value=str(SCHEMA_VERSION), updated_at=now
                    )
                )
            elif existing != str(SCHEMA_VERSION):
                conn.execute(
                    update(meta_table)
                    .where(meta_table.c.key == "schema_version")
                    .values(value=str(SCHEMA_VERSION), updated_at=now)
                )

    def _safe_url(self) -> str:
        """URL with any password redacted, for logs."""
        try:
            from sqlalchemy.engine import make_url

            return make_url(self.url).render_as_string(hide_password=True)
        except Exception:
            return "<metastore>"

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._ready = False

    def __enter__(self) -> Metastore:
        self.bootstrap()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _guard(self, action: str, exc: Exception) -> None:
        """Either raise or log, depending on strict mode."""
        if self.strict:
            raise MetastoreError(f"Metastore {action} failed: {exc}") from exc
        log.warning("Metastore %s failed (continuing): %s", action, exc)

    # -- upserts ------------------------------------------------------------ #

    def _upsert(
        self,
        table,
        key_columns: dict[str, Any],
        values: dict[str, Any],
        insert_only: dict[str, Any] | None = None,
    ) -> None:
        """Portable upsert: UPDATE, and INSERT only when nothing was touched.

        Deliberately avoids ``ON CONFLICT`` / ``MERGE`` so the same code path
        works on SQLite, Postgres, MySQL and SQL Server without dialect
        branching.

        Args:
            insert_only: Columns written on INSERT but never on UPDATE. This is
                how ``first_seen_at`` / ``created_at`` survive later updates
                while still satisfying their NOT NULL constraint on insert.
        """
        where = [table.c[col] == val for col, val in key_columns.items()]
        with self.engine.begin() as conn:
            updated = conn.execute(update(table).where(*where).values(**values)).rowcount
            if not updated:
                conn.execute(
                    table.insert().values(**{**key_columns, **values, **(insert_only or {})})
                )

    def register_connection(
        self,
        config: ConnectionConfig,
        *,
        server_version: str | None = None,
        status: str = "connected",
        error: str | None = None,
    ) -> str | None:
        """Record a connection. Credentials are never written."""
        if not self.bootstrap():
            return None
        now = utcnow()
        conn_id = _hash_id("conn", config.name)
        try:
            self._upsert(
                connections_table,
                {"id": conn_id},
                {
                    "name": config.name,
                    "type": config.type,
                    "fingerprint": config.fingerprint(),
                    "description": config.description,
                    "host": config.host,
                    "port": config.port,
                    "database": config.database,
                    "schema_name": config.db_schema,
                    "account": config.account,
                    "warehouse": config.warehouse,
                    "server_version": (server_version or "")[:512] or None,
                    "tags": _dumps(config.tags),
                    "last_seen_at": now,
                    "last_status": status,
                    "last_error": error,
                },
                insert_only={"first_seen_at": now},
            )
        except SQLAlchemyError as exc:
            self._guard("register_connection", exc)
            return None
        return conn_id

    def upsert_dataset(self, connection_name: str, dataset: DatasetInfo) -> str | None:
        """Record a table and its columns; refresh ``last_seen_at`` on both."""
        if not self.bootstrap():
            return None
        now = utcnow()
        fqn = dataset.ref.fqn
        dataset_id = _hash_id("ds", connection_name, fqn.lower())
        try:
            self._upsert(
                datasets_table,
                {"id": dataset_id},
                {
                    "connection_name": connection_name,
                    "database": dataset.ref.database,
                    "schema_name": dataset.ref.db_schema,
                    "table_name": dataset.ref.table,
                    "fqn": fqn,
                    "object_type": dataset.object_type,
                    "column_count": len(dataset.columns),
                    "row_count": dataset.row_count,
                    "size_bytes": dataset.size_bytes,
                    "comment": dataset.comment,
                    "last_seen_at": now,
                },
                insert_only={"first_seen_at": now},
            )

            for column in dataset.columns:
                column_id = _hash_id("col", dataset_id, column.name.lower())
                self._upsert(
                    columns_table,
                    {"id": column_id},
                    {
                        "dataset_id": dataset_id,
                        "connection_name": connection_name,
                        "dataset_fqn": fqn,
                        "name": column.name,
                        "ordinal": column.ordinal,
                        "data_type": column.data_type[:255],
                        "kind": column.kind.value,
                        "nullable": column.nullable,
                        "primary_key": column.primary_key,
                        "default_value": column.default,
                        "comment": column.comment,
                        "last_seen_at": now,
                    },
                    insert_only={"first_seen_at": now},
                )
        except SQLAlchemyError as exc:
            self._guard("upsert_dataset", exc)
            return None
        return dataset_id

    def set_dataset_description(self, connection_name: str, fqn: str, description: str) -> bool:
        """Attach a business description to a discovered table."""
        if not self.bootstrap():
            return False
        try:
            with self.engine.begin() as conn:
                rows = conn.execute(
                    update(datasets_table)
                    .where(
                        datasets_table.c.connection_name == connection_name,
                        datasets_table.c.fqn == fqn,
                    )
                    .values(description=description)
                ).rowcount
            return bool(rows)
        except SQLAlchemyError as exc:
            self._guard("set_dataset_description", exc)
            return False

    def set_column_description(
        self, connection_name: str, fqn: str, column: str, description: str
    ) -> bool:
        if not self.bootstrap():
            return False
        try:
            with self.engine.begin() as conn:
                rows = conn.execute(
                    update(columns_table)
                    .where(
                        columns_table.c.connection_name == connection_name,
                        columns_table.c.dataset_fqn == fqn,
                        func.lower(columns_table.c.name) == column.lower(),
                    )
                    .values(description=description)
                ).rowcount
            return bool(rows)
        except SQLAlchemyError as exc:
            self._guard("set_column_description", exc)
            return False

    def sync_suite(self, suite: Suite) -> int:
        """Mirror a suite definition into ``nexassure_checks``.

        Checks removed from the file are removed from the metastore, so the
        registry always reflects what is actually declared.
        """
        if not self.bootstrap():
            return 0
        now = utcnow()
        written = 0
        try:
            for check in suite.checks:
                self._write_check(suite, check, now)
                written += 1
            live = {c.name for c in suite.checks}
            with self.engine.begin() as conn:
                existing = (
                    conn.execute(
                        select(checks_table.c.name).where(checks_table.c.suite_name == suite.name)
                    )
                    .scalars()
                    .all()
                )
                stale = set(existing) - live
                if stale:
                    conn.execute(
                        delete(checks_table).where(
                            checks_table.c.suite_name == suite.name,
                            checks_table.c.name.in_(stale),
                        )
                    )
        except SQLAlchemyError as exc:
            self._guard("sync_suite", exc)
        return written

    def _write_check(self, suite: Suite, check: CheckSpec, now: datetime) -> None:
        row_id = _hash_id("chk", suite.name, check.name)
        self._upsert(
            checks_table,
            {"id": row_id},
            {
                "suite_name": suite.name,
                "connection_name": suite.connection,
                "name": check.name,
                "type": check.type,
                "description": check.description,
                "dataset_fqn": check.dataset.fqn if check.dataset else None,
                "column_name": check.column,
                "query": check.query,
                "expectation": _dumps(check.expect.model_dump(mode="json"))
                if check.expect
                else None,
                "params": _dumps(check.params),
                "severity": check.severity.value,
                "threshold": check.threshold,
                "enabled": check.enabled,
                "owner": check.owner,
                "tags": _dumps(check.tags),
                "source_path": suite.source_path,
                "updated_at": now,
            },
            insert_only={"created_at": now},
        )

    def record_run(self, run: RunResult) -> bool:
        """Persist a completed run plus all of its check results."""
        if not self.bootstrap():
            return False
        try:
            with self.engine.begin() as conn:
                conn.execute(delete(runs_table).where(runs_table.c.run_id == run.run_id))
                conn.execute(delete(results_table).where(results_table.c.run_id == run.run_id))
                conn.execute(
                    runs_table.insert().values(
                        run_id=run.run_id,
                        suite_name=run.suite_name,
                        connection_name=run.connection_name,
                        status=run.status.value,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                        duration_ms=run.duration_ms,
                        total=run.summary.total,
                        passed=run.summary.passed,
                        failed=run.summary.failed,
                        warned=run.summary.warned,
                        errored=run.summary.errored,
                        skipped=run.summary.skipped,
                        pass_rate=run.summary.pass_rate,
                        triggered_by=run.triggered_by,
                        environment=run.environment,
                        error=run.error,
                        run_metadata=_dumps(run.metadata),
                    )
                )
                if run.results:
                    conn.execute(
                        results_table.insert(),
                        [
                            {
                                "id": _hash_id("res", run.run_id, r.check_id, i),
                                "run_id": run.run_id,
                                "check_id": r.check_id,
                                "check_name": r.check_name,
                                "check_type": r.check_type,
                                "suite_name": run.suite_name,
                                "connection_name": run.connection_name,
                                "status": r.status.value,
                                "severity": r.severity.value,
                                "description": r.description,
                                "dataset_fqn": r.dataset,
                                "column_name": r.column,
                                "observed": _dumps(r.observed),
                                "expected": _dumps(r.expected),
                                "message": r.message,
                                "rows_scanned": r.rows_scanned,
                                "rows_failed": r.rows_failed,
                                "failed_ratio": r.failed_ratio,
                                "sample_rows": _dumps(r.sample_rows),
                                "query": r.query,
                                "duration_ms": r.duration_ms,
                                "started_at": r.started_at,
                                "error": r.error,
                                "tags": _dumps(r.tags),
                                "owner": r.owner,
                            }
                            for i, r in enumerate(run.results)
                        ],
                    )
        except SQLAlchemyError as exc:
            self._guard("record_run", exc)
            return False
        return True

    def record_profile(self, profile: TableProfile) -> bool:
        """Persist a table profile and each of its column profiles."""
        if not self.bootstrap():
            return False
        fqn = profile.dataset.fqn
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    profiles_table.insert().values(
                        profile_id=profile.profile_id,
                        connection_name=profile.connection_name,
                        dataset_fqn=fqn,
                        row_count=profile.row_count,
                        column_count=profile.column_count,
                        duplicate_row_count=profile.duplicate_row_count,
                        size_bytes=profile.size_bytes,
                        sampled=profile.sampled,
                        sample_size=profile.sample_size,
                        profiled_at=profile.profiled_at,
                        duration_ms=profile.duration_ms,
                    )
                )
                if profile.columns:
                    conn.execute(
                        column_profiles_table.insert(),
                        [self._column_profile_row(profile, cp, fqn) for cp in profile.columns],
                    )
        except SQLAlchemyError as exc:
            self._guard("record_profile", exc)
            return False
        return True

    @staticmethod
    def _column_profile_row(profile: TableProfile, cp: ColumnProfile, fqn: str) -> dict[str, Any]:
        return {
            "id": _hash_id("cp", profile.profile_id, cp.column.lower()),
            "profile_id": profile.profile_id,
            "connection_name": profile.connection_name,
            "dataset_fqn": fqn,
            "column_name": cp.column,
            "data_type": (cp.data_type or "")[:255],
            "kind": cp.kind.value,
            "row_count": cp.row_count,
            "null_count": cp.null_count,
            "null_ratio": cp.null_ratio,
            "distinct_count": cp.distinct_count,
            "distinct_ratio": cp.distinct_ratio,
            "duplicate_count": cp.duplicate_count,
            "is_unique": cp.is_unique,
            "blank_count": cp.blank_count,
            "zero_count": cp.zero_count,
            "min_value": None if cp.min is None else str(cp.min)[:4000],
            "max_value": None if cp.max is None else str(cp.max)[:4000],
            "mean": cp.mean,
            "stddev": cp.stddev,
            "sum_value": cp.sum,
            "median": cp.median,
            "p25": cp.p25,
            "p75": cp.p75,
            "p95": cp.p95,
            "min_length": cp.min_length,
            "max_length": cp.max_length,
            "avg_length": cp.avg_length,
            "top_values": _dumps(cp.top_values),
            "completeness": cp.completeness,
            "profiled_at": profile.profiled_at,
        }

    # -- schedules ---------------------------------------------------------- #

    def upsert_schedule(
        self,
        name: str,
        suite_name: str,
        cron: str,
        *,
        suite_path: str | None = None,
        timezone: str = "UTC",
        enabled: bool = True,
        next_run_at: datetime | None = None,
    ) -> str | None:
        if not self.bootstrap():
            return None
        now = utcnow()
        row_id = _hash_id("sched", name)
        try:
            self._upsert(
                schedules_table,
                {"id": row_id},
                {
                    "name": name,
                    "suite_name": suite_name,
                    "suite_path": suite_path,
                    "cron": cron,
                    "timezone": timezone,
                    "enabled": enabled,
                    "next_run_at": next_run_at,
                    "updated_at": now,
                },
                insert_only={"created_at": now},
            )
        except SQLAlchemyError as exc:
            self._guard("upsert_schedule", exc)
            return None
        return row_id

    def mark_schedule_run(
        self, name: str, run_id: str, status: str, next_run_at: datetime | None = None
    ) -> None:
        if not self.bootstrap():
            return
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    update(schedules_table)
                    .where(schedules_table.c.name == name)
                    .values(
                        last_run_at=utcnow(),
                        last_run_id=run_id,
                        last_status=status,
                        next_run_at=next_run_at,
                    )
                )
        except SQLAlchemyError as exc:
            self._guard("mark_schedule_run", exc)

    def list_schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        if not self.bootstrap():
            return []
        stmt = select(schedules_table)
        if enabled_only:
            stmt = stmt.where(schedules_table.c.enabled.is_(True))
        return self._rows(stmt.order_by(schedules_table.c.name))

    def delete_schedule(self, name: str) -> bool:
        if not self.bootstrap():
            return False
        try:
            with self.engine.begin() as conn:
                return bool(
                    conn.execute(
                        delete(schedules_table).where(schedules_table.c.name == name)
                    ).rowcount
                )
        except SQLAlchemyError as exc:
            self._guard("delete_schedule", exc)
            return False

    # -- queries ------------------------------------------------------------ #

    def _rows(self, stmt) -> list[dict[str, Any]]:
        try:
            with self.engine.connect() as conn:
                return [dict(r._mapping) for r in conn.execute(stmt)]
        except SQLAlchemyError as exc:
            self._guard("query", exc)
            return []

    def list_connections(self) -> list[dict[str, Any]]:
        if not self.bootstrap():
            return []
        return self._rows(select(connections_table).order_by(connections_table.c.name))

    def list_datasets(
        self, connection_name: str | None = None, schema: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        if not self.bootstrap():
            return []
        stmt = select(datasets_table)
        if connection_name:
            stmt = stmt.where(datasets_table.c.connection_name == connection_name)
        if schema:
            stmt = stmt.where(datasets_table.c.schema_name == schema)
        return self._rows(stmt.order_by(datasets_table.c.fqn).limit(limit))

    def get_dataset_columns(self, connection_name: str, fqn: str) -> list[dict[str, Any]]:
        if not self.bootstrap():
            return []
        return self._rows(
            select(columns_table)
            .where(
                columns_table.c.connection_name == connection_name,
                columns_table.c.dataset_fqn == fqn,
            )
            .order_by(columns_table.c.ordinal)
        )

    def list_checks(
        self, suite_name: str | None = None, dataset_fqn: str | None = None
    ) -> list[dict[str, Any]]:
        if not self.bootstrap():
            return []
        stmt = select(checks_table)
        if suite_name:
            stmt = stmt.where(checks_table.c.suite_name == suite_name)
        if dataset_fqn:
            stmt = stmt.where(checks_table.c.dataset_fqn == dataset_fqn)
        rows = self._rows(stmt.order_by(checks_table.c.suite_name, checks_table.c.name))
        for row in rows:
            row["expectation"] = _loads(row.get("expectation"))
            row["params"] = _loads(row.get("params"))
            row["tags"] = _loads(row.get("tags")) or []
        return rows

    def list_runs(
        self, suite_name: str | None = None, limit: int = 50, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        if not self.bootstrap():
            return []
        stmt = select(runs_table)
        if suite_name:
            stmt = stmt.where(runs_table.c.suite_name == suite_name)
        if since:
            stmt = stmt.where(runs_table.c.started_at >= since)
        return self._rows(stmt.order_by(runs_table.c.started_at.desc()).limit(limit))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        if not self.bootstrap():
            return None
        runs = self._rows(select(runs_table).where(runs_table.c.run_id == run_id))
        if not runs:
            return None
        run = runs[0]
        run["results"] = self.get_run_results(run_id)
        run["run_metadata"] = _loads(run.get("run_metadata"))
        return run

    def get_run_results(self, run_id: str, status: str | None = None) -> list[dict[str, Any]]:
        if not self.bootstrap():
            return []
        stmt = select(results_table).where(results_table.c.run_id == run_id)
        if status:
            stmt = stmt.where(results_table.c.status == status)
        rows = self._rows(stmt.order_by(results_table.c.check_name))
        for row in rows:
            row["observed"] = _loads(row.get("observed"))
            row["expected"] = _loads(row.get("expected"))
            row["sample_rows"] = _loads(row.get("sample_rows")) or []
            row["tags"] = _loads(row.get("tags")) or []
        return rows

    def check_history(self, check_id: str, limit: int = 30) -> list[dict[str, Any]]:
        """Recent outcomes for one check - the data behind a sparkline."""
        if not self.bootstrap():
            return []
        return self._rows(
            select(
                results_table.c.run_id,
                results_table.c.status,
                results_table.c.observed,
                results_table.c.rows_failed,
                results_table.c.duration_ms,
                results_table.c.started_at,
            )
            .where(results_table.c.check_id == check_id)
            .order_by(results_table.c.started_at.desc())
            .limit(limit)
        )

    def profile_history(
        self, dataset_fqn: str, column: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Profiling snapshots over time, for drift detection."""
        if not self.bootstrap():
            return []
        if column:
            stmt = (
                select(column_profiles_table)
                .where(
                    column_profiles_table.c.dataset_fqn == dataset_fqn,
                    func.lower(column_profiles_table.c.column_name) == column.lower(),
                )
                .order_by(column_profiles_table.c.profiled_at.desc())
                .limit(limit)
            )
        else:
            stmt = (
                select(profiles_table)
                .where(profiles_table.c.dataset_fqn == dataset_fqn)
                .order_by(profiles_table.c.profiled_at.desc())
                .limit(limit)
            )
        return self._rows(stmt)

    def latest_profile(self, dataset_fqn: str) -> dict[str, Any] | None:
        rows = self.profile_history(dataset_fqn, limit=1)
        if not rows:
            return None
        profile = rows[0]
        profile["columns"] = self._rows(
            select(column_profiles_table).where(
                column_profiles_table.c.profile_id == profile["profile_id"]
            )
        )
        return profile

    def failing_checks(self, since_hours: int = 24, limit: int = 100) -> list[dict[str, Any]]:
        """Everything that failed recently, newest first."""
        if not self.bootstrap():
            return []
        cutoff = utcnow() - timedelta(hours=since_hours)
        return self._rows(
            select(results_table)
            .where(
                results_table.c.started_at >= cutoff,
                results_table.c.status.in_(("failed", "errored")),
            )
            .order_by(results_table.c.started_at.desc())
            .limit(limit)
        )

    def summary(self, since_hours: int = 24) -> dict[str, Any]:
        """Headline numbers for a dashboard or an agent asking how things look."""
        if not self.bootstrap():
            return {}
        cutoff = utcnow() - timedelta(hours=since_hours)
        try:
            with self.engine.connect() as conn:
                totals = (
                    conn.execute(
                        select(
                            func.count().label("runs"),
                            func.coalesce(func.sum(runs_table.c.total), 0).label("checks"),
                            func.coalesce(func.sum(runs_table.c.passed), 0).label("passed"),
                            func.coalesce(func.sum(runs_table.c.failed), 0).label("failed"),
                            func.coalesce(func.sum(runs_table.c.errored), 0).label("errored"),
                        ).where(runs_table.c.started_at >= cutoff)
                    )
                    .one()
                    ._mapping
                )
                datasets = conn.execute(select(func.count()).select_from(datasets_table)).scalar()
                connections = conn.execute(
                    select(func.count()).select_from(connections_table)
                ).scalar()
                checks = conn.execute(select(func.count()).select_from(checks_table)).scalar()
        except SQLAlchemyError as exc:
            self._guard("summary", exc)
            return {}

        executed = int(totals["checks"] or 0)
        return {
            "window_hours": since_hours,
            "runs": int(totals["runs"] or 0),
            "checks_executed": executed,
            "passed": int(totals["passed"] or 0),
            "failed": int(totals["failed"] or 0),
            "errored": int(totals["errored"] or 0),
            "pass_rate": (int(totals["passed"] or 0) / executed) if executed else None,
            "registered_connections": int(connections or 0),
            "registered_datasets": int(datasets or 0),
            "registered_checks": int(checks or 0),
        }

    def purge(self, older_than_days: int = 90) -> dict[str, int]:
        """Delete history older than ``older_than_days``. Returns rows removed per table."""
        if not self.bootstrap():
            return {}
        cutoff = utcnow() - timedelta(days=older_than_days)
        removed: dict[str, int] = {}
        try:
            with self.engine.begin() as conn:
                stale_runs = (
                    conn.execute(
                        select(runs_table.c.run_id).where(runs_table.c.started_at < cutoff)
                    )
                    .scalars()
                    .all()
                )
                if stale_runs:
                    removed["check_results"] = conn.execute(
                        delete(results_table).where(results_table.c.run_id.in_(stale_runs))
                    ).rowcount
                    removed["runs"] = conn.execute(
                        delete(runs_table).where(runs_table.c.run_id.in_(stale_runs))
                    ).rowcount

                stale_profiles = (
                    conn.execute(
                        select(profiles_table.c.profile_id).where(
                            profiles_table.c.profiled_at < cutoff
                        )
                    )
                    .scalars()
                    .all()
                )
                if stale_profiles:
                    removed["column_profiles"] = conn.execute(
                        delete(column_profiles_table).where(
                            column_profiles_table.c.profile_id.in_(stale_profiles)
                        )
                    ).rowcount
                    removed["profiles"] = conn.execute(
                        delete(profiles_table).where(
                            profiles_table.c.profile_id.in_(stale_profiles)
                        )
                    ).rowcount
        except SQLAlchemyError as exc:
            self._guard("purge", exc)
        return removed


def bootstrap_on_connect(
    metastore: Metastore,
    connector: Any,
    *,
    discover: bool = True,
    schemas: Iterable[str] | None = None,
    max_tables: int = 200,
) -> dict[str, Any]:
    """Register a connection and, optionally, catalog what lives on it.

    This is the hook that satisfies "the metadata tables appear as soon as you
    connect a driver": :meth:`Metastore.bootstrap` creates the NexAssure tables,
    the connection is recorded, and each discovered table and column is upserted
    into ``nexassure_datasets`` / ``nexassure_columns``.

    Discovery is capped by ``max_tables`` because pointing NexAssure at a warehouse
    with 50,000 tables should not turn a connection test into a ten-minute
    catalog crawl.
    """
    config = connector.config
    metastore.register_connection(config, server_version=connector.server_version())

    outcome: dict[str, Any] = {
        "connection": config.name,
        "connector": config.type,
        "metastore": metastore._safe_url(),
        "datasets_registered": 0,
        "columns_registered": 0,
        "schemas_scanned": [],
        "truncated": False,
    }
    if not discover:
        return outcome

    targets = list(schemas) if schemas else ([config.db_schema] if config.db_schema else [None])
    seen = 0
    for schema in targets:
        try:
            tables = connector.list_tables(schema=schema)
        except Exception as exc:
            log.warning("Discovery failed for schema %s: %s", schema, exc)
            continue
        outcome["schemas_scanned"].append(schema)

        for ref in tables:
            if seen >= max_tables:
                outcome["truncated"] = True
                break
            try:
                dataset = connector.describe_table(ref)
            except Exception as exc:
                log.debug("Skipping %s: %s", ref.fqn, exc)
                continue
            if metastore.upsert_dataset(config.name, dataset):
                outcome["datasets_registered"] += 1
                outcome["columns_registered"] += len(dataset.columns)
            seen += 1
        if outcome["truncated"]:
            break

    log.info(
        "Registered %s datasets (%s columns) for connection %s",
        outcome["datasets_registered"],
        outcome["columns_registered"],
        config.name,
    )
    return outcome
