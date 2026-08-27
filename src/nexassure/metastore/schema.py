# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Metastore table definitions.

The metastore is where NexAssure keeps what it learns: every connection it has
seen, every table and column discovered on that connection, every check that
has been declared, and the full history of runs, results and profiles.

It is deliberately a *separate* database from the warehouse under test. The
default is a SQLite file under ``~/.nexassure``, which needs no setup at all;
teams that want shared history point ``NEXASSURE_METASTORE_URL`` at Postgres.

Tables are created on demand the first time a connection is opened, so there is
no migration step to run before the first ``nexassure run``.

Columns use portable types only (no JSONB, no arrays), because the same DDL has
to work on SQLite, Postgres, MySQL and SQL Server. Structured values are stored
as JSON text and (de)serialised in :mod:`nexassure.metastore.repository`.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

#: Prefix on every NexAssure-owned table, so the metastore can share a schema.
TABLE_PREFIX = "nexassure_"

#: Naming convention keeps generated constraint names stable across backends,
#: which is what makes future Alembic migrations able to find them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

#: Schema version written into ``nexassure_meta``; bumped when the DDL changes.
SCHEMA_VERSION = 1


meta_table = Table(
    f"{TABLE_PREFIX}meta",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    comment="Key/value store for metastore schema version and install metadata",
)


connections_table = Table(
    f"{TABLE_PREFIX}connections",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(255), nullable=False, unique=True),
    Column("type", String(64), nullable=False),
    Column("fingerprint", String(32), nullable=False),
    Column("description", Text),
    Column("host", String(512)),
    Column("port", Integer),
    Column("database", String(255)),
    Column("schema_name", String(255)),
    Column("account", String(255)),
    Column("warehouse", String(255)),
    Column("server_version", String(512)),
    Column("tags", Text),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_status", String(32)),
    Column("last_error", Text),
    comment="Every data source NexAssure has connected to. Never stores credentials.",
)


datasets_table = Table(
    f"{TABLE_PREFIX}datasets",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("connection_name", String(255), nullable=False, index=True),
    Column("database", String(255)),
    Column("schema_name", String(255)),
    Column("table_name", String(255), nullable=False),
    Column("fqn", String(1024), nullable=False),
    Column("object_type", String(32), nullable=False, default="table"),
    Column("column_count", Integer, default=0),
    Column("row_count", Integer),
    Column("size_bytes", Integer),
    Column("description", Text, comment="User-supplied business description"),
    Column("comment", Text, comment="Comment read from the source catalog"),
    Column("owner", String(255)),
    Column("tags", Text),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("connection_name", "fqn", name="uq_dataset_conn_fqn"),
    Index("ix_na_datasets_fqn", "fqn"),
    comment="Tables and views discovered by catalog introspection",
)


columns_table = Table(
    f"{TABLE_PREFIX}columns",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("dataset_id", String(32), nullable=False, index=True),
    Column("connection_name", String(255), nullable=False),
    Column("dataset_fqn", String(1024), nullable=False),
    Column("name", String(255), nullable=False),
    Column("ordinal", Integer, default=0),
    Column("data_type", String(255)),
    Column("kind", String(32)),
    Column("nullable", Boolean, default=True),
    Column("primary_key", Boolean, default=False),
    Column("default_value", Text),
    Column("description", Text, comment="User-supplied business description"),
    Column("comment", Text, comment="Comment read from the source catalog"),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("dataset_id", "name", name="uq_column_dataset_name"),
    comment="Columns discovered per dataset, with drift timestamps",
)


checks_table = Table(
    f"{TABLE_PREFIX}checks",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("suite_name", String(255), nullable=False, index=True),
    Column("connection_name", String(255), nullable=False),
    Column("name", String(255), nullable=False),
    Column("type", String(64), nullable=False),
    Column("description", Text),
    Column("dataset_fqn", String(1024), index=True),
    Column("column_name", String(255)),
    Column("query", Text),
    Column("expectation", Text, comment="JSON-encoded Expectation"),
    Column("params", Text, comment="JSON-encoded check params"),
    Column("severity", String(16), nullable=False, default="error"),
    Column("threshold", Float),
    Column("enabled", Boolean, default=True),
    Column("owner", String(255)),
    Column("tags", Text),
    Column("source_path", String(1024)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("suite_name", "name", name="uq_check_suite_name"),
    comment="Registered check definitions, kept in sync from suite files",
)


runs_table = Table(
    f"{TABLE_PREFIX}runs",
    metadata,
    Column("run_id", String(32), primary_key=True),
    Column("suite_name", String(255), nullable=False, index=True),
    Column("connection_name", String(255), nullable=False, index=True),
    Column("status", String(32), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, index=True),
    Column("finished_at", DateTime(timezone=True)),
    Column("duration_ms", Float, default=0.0),
    Column("total", Integer, default=0),
    Column("passed", Integer, default=0),
    Column("failed", Integer, default=0),
    Column("warned", Integer, default=0),
    Column("errored", Integer, default=0),
    Column("skipped", Integer, default=0),
    Column("pass_rate", Float),
    Column("triggered_by", String(64), default="manual"),
    Column("environment", String(64)),
    Column("error", Text),
    Column("run_metadata", Text, comment="JSON blob of arbitrary run context"),
    comment="One row per suite execution",
)


results_table = Table(
    f"{TABLE_PREFIX}check_results",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("run_id", String(32), nullable=False, index=True),
    Column("check_id", String(32), nullable=False, index=True),
    Column("check_name", String(255), nullable=False),
    Column("check_type", String(64), nullable=False),
    Column("suite_name", String(255), nullable=False),
    Column("connection_name", String(255), nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("severity", String(16), nullable=False),
    Column("description", Text),
    Column("dataset_fqn", String(1024), index=True),
    Column("column_name", String(255)),
    Column("observed", Text, comment="JSON-encoded observed value"),
    Column("expected", Text, comment="JSON-encoded expected value"),
    Column("message", Text),
    Column("rows_scanned", Integer),
    Column("rows_failed", Integer),
    Column("failed_ratio", Float),
    Column("sample_rows", Text, comment="JSON array of failing rows, for triage"),
    Column("query", Text),
    Column("duration_ms", Float, default=0.0),
    Column("started_at", DateTime(timezone=True), nullable=False, index=True),
    Column("error", Text),
    Column("tags", Text),
    Column("owner", String(255)),
    Index("ix_na_results_check_time", "check_id", "started_at"),
    comment="One row per check per run - the table trend queries read from",
)


profiles_table = Table(
    f"{TABLE_PREFIX}profiles",
    metadata,
    Column("profile_id", String(32), primary_key=True),
    Column("connection_name", String(255), nullable=False, index=True),
    Column("dataset_fqn", String(1024), nullable=False, index=True),
    Column("row_count", Integer, default=0),
    Column("column_count", Integer, default=0),
    Column("duplicate_row_count", Integer),
    Column("size_bytes", Integer),
    Column("sampled", Boolean, default=False),
    Column("sample_size", Integer),
    Column("profiled_at", DateTime(timezone=True), nullable=False, index=True),
    Column("duration_ms", Float, default=0.0),
    comment="Table-level profiling snapshots",
)


column_profiles_table = Table(
    f"{TABLE_PREFIX}column_profiles",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("profile_id", String(32), nullable=False, index=True),
    Column("connection_name", String(255), nullable=False),
    Column("dataset_fqn", String(1024), nullable=False, index=True),
    Column("column_name", String(255), nullable=False),
    Column("data_type", String(255)),
    Column("kind", String(32)),
    Column("row_count", Integer, default=0),
    Column("null_count", Integer, default=0),
    Column("null_ratio", Float, default=0.0),
    Column("distinct_count", Integer),
    Column("distinct_ratio", Float),
    Column("duplicate_count", Integer),
    Column("is_unique", Boolean),
    Column("blank_count", Integer),
    Column("zero_count", Integer),
    Column("min_value", Text),
    Column("max_value", Text),
    Column("mean", Float),
    Column("stddev", Float),
    Column("sum_value", Float),
    Column("median", Float),
    Column("p25", Float),
    Column("p75", Float),
    Column("p95", Float),
    Column("min_length", Integer),
    Column("max_length", Integer),
    Column("avg_length", Float),
    Column("top_values", Text, comment="JSON array of {value, count}"),
    Column("completeness", Float, default=1.0),
    Column("profiled_at", DateTime(timezone=True), nullable=False, index=True),
    Index("ix_na_colprof_trend", "dataset_fqn", "column_name", "profiled_at"),
    comment="Column-level profiling snapshots - the basis for drift detection",
)


schedules_table = Table(
    f"{TABLE_PREFIX}schedules",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(255), nullable=False, unique=True),
    Column("suite_name", String(255), nullable=False),
    Column("suite_path", String(1024)),
    Column("cron", String(128), nullable=False),
    Column("timezone", String(64), default="UTC"),
    Column("enabled", Boolean, default=True),
    Column("last_run_at", DateTime(timezone=True)),
    Column("last_run_id", String(32)),
    Column("last_status", String(32)),
    Column("next_run_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    comment="Cron schedules managed by the NexAssure scheduler",
)


#: Created in dependency order (there are no FKs, but order keeps DDL readable).
ALL_TABLES = (
    meta_table,
    connections_table,
    datasets_table,
    columns_table,
    checks_table,
    runs_table,
    results_table,
    profiles_table,
    column_profiles_table,
    schedules_table,
)
