# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Data profiling.

Profiling answers "what is actually in this table?" - row counts, null rates,
cardinality, ranges, string lengths, and the most common values.

The cost model matters more than the metric list. A naive profiler issues one
query per metric per column, which on a 200-column table is thousands of
warehouse round trips. This one batches every aggregate for a group of columns
into a single ``SELECT``, so a wide table costs a handful of scans instead:

* 1 query for table-level counts
* ceil(columns / batch_size) queries for column aggregates
* 1 query per column only for the optional extras (percentiles, top values)

Everything degrades gracefully. If a dialect cannot compute a metric, or a
column type rejects an aggregate, that metric comes back ``None`` rather than
failing the whole profile.
"""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from ..connectors.base import BaseConnector
from ..core.enums import ColumnKind
from ..core.models import ColumnInfo, ColumnProfile, TableProfile, TableRef
from ..logging_conf import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ProfileOptions:
    """Knobs controlling how much work a profile does."""

    #: Columns whose aggregates share one query. Above ~50, some engines start
    #: rejecting the statement for having too many expressions.
    batch_size: int = 25
    #: Compute median/p25/p75/p95 for numeric columns. One extra query each.
    include_percentiles: bool = False
    #: Collect the most frequent values. One extra query per column.
    include_top_values: bool = True
    top_values_limit: int = 10
    #: Count fully duplicated rows. A full GROUP BY on every column - expensive.
    include_duplicate_rows: bool = False
    #: Profile a sample rather than the whole table, for very large tables.
    sample_rows: int | None = None
    #: Skip these columns entirely (e.g. a huge BLOB payload).
    exclude_columns: list[str] = field(default_factory=list)
    #: Only profile these columns, when set.
    include_columns: list[str] = field(default_factory=list)
    #: Row-level filter applied to every query.
    where: str | None = None
    #: Skip distinct counts on these kinds; distinct on a BLOB is rarely useful.
    skip_distinct_kinds: tuple[ColumnKind, ...] = (ColumnKind.BINARY, ColumnKind.JSON)


class Profiler:
    """Profiles tables on one connection."""

    def __init__(self, connector: BaseConnector, options: ProfileOptions | None = None) -> None:
        self.connector = connector
        self.options = options or ProfileOptions()

    # -- public API --------------------------------------------------------- #

    def profile_table(self, ref: TableRef) -> TableProfile:
        """Profile one table and return the full snapshot."""
        started = time.perf_counter()
        dialect = self.connector.dialect
        dataset = self.connector.describe_table(ref)
        columns = self._select_columns(dataset.columns)

        source = self._source_expression(dataset.ref)
        scope = f" WHERE {self.options.where}" if self.options.where else ""

        row_count = int(self.connector.scalar(f"SELECT COUNT(*) FROM {source}{scope}") or 0)

        profile = TableProfile(
            dataset=dataset.ref,
            connection_name=self.connector.config.name,
            row_count=row_count,
            column_count=len(dataset.columns),
            sampled=self.options.sample_rows is not None,
            sample_size=self.options.sample_rows,
        )

        if self.options.include_duplicate_rows and columns and row_count:
            profile.duplicate_row_count = self._count_duplicate_rows(source, scope, columns)

        for batch in _chunks(columns, self.options.batch_size):
            profile.columns.extend(self._profile_batch(source, scope, batch, row_count))

        if self.options.include_percentiles and dialect.supports_percentile:
            self._add_percentiles(profile, source, scope, columns)

        if self.options.include_top_values and row_count:
            self._add_top_values(profile, source, scope, columns)

        profile.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        log.info(
            "Profiled %s: %s rows, %s columns in %.0fms",
            dataset.ref.fqn,
            row_count,
            len(profile.columns),
            profile.duration_ms,
        )
        return profile

    def profile_schema(self, schema: str | None = None, limit: int = 50) -> list[TableProfile]:
        """Profile every table in a schema, up to ``limit``."""
        profiles = []
        for ref in self.connector.list_tables(schema=schema)[:limit]:
            try:
                profiles.append(self.profile_table(ref))
            except Exception as exc:
                log.warning("Could not profile %s: %s", ref.fqn, exc)
        return profiles

    # -- internals ---------------------------------------------------------- #

    def _select_columns(self, columns: list[ColumnInfo]) -> list[ColumnInfo]:
        excluded = {c.lower() for c in self.options.exclude_columns}
        included = {c.lower() for c in self.options.include_columns}
        chosen = [c for c in columns if c.name.lower() not in excluded]
        if included:
            chosen = [c for c in chosen if c.name.lower() in included]
        return chosen

    def _source_expression(self, ref: TableRef) -> str:
        """The FROM target, wrapped in a sampling subquery when asked.

        A plain ``LIMIT`` subquery is used rather than ``TABLESAMPLE`` because
        it behaves identically everywhere; ``TABLESAMPLE`` semantics differ
        sharply between engines and is missing on Synapse and Redshift.
        """
        table = self.connector.dialect.qualify(ref)
        if self.options.sample_rows is None:
            return table
        limited = self.connector.dialect.limit(f"SELECT * FROM {table}", self.options.sample_rows)
        return f"({limited}) AS _na_sample"

    def _count_duplicate_rows(
        self, source: str, scope: str, columns: list[ColumnInfo]
    ) -> int | None:
        grouped = ", ".join(self.connector.dialect.quote(c.name) for c in columns)
        sql = (
            f"SELECT COALESCE(SUM(group_size - 1), 0) FROM "
            f"(SELECT {grouped}, COUNT(*) AS group_size FROM {source}{scope} "
            f"GROUP BY {grouped} HAVING COUNT(*) > 1) AS dup"
        )
        try:
            return int(self.connector.scalar(sql) or 0)
        except Exception as exc:
            log.debug("Duplicate row count unavailable: %s", exc)
            return None

    def _profile_batch(
        self, source: str, scope: str, batch: list[ColumnInfo], row_count: int
    ) -> list[ColumnProfile]:
        """One query computing every aggregate for a group of columns."""
        dialect = self.connector.dialect
        selects: list[str] = ["COUNT(*) AS _total"]
        # Alias by position, because column names can collide with SQL keywords
        # or exceed identifier length limits once suffixed.
        for i, column in enumerate(batch):
            quoted = dialect.quote(column.name)
            selects.append(f"COUNT({quoted}) AS c{i}_nonnull")

            if column.kind not in self.options.skip_distinct_kinds:
                selects.append(f"COUNT(DISTINCT {quoted}) AS c{i}_distinct")
            else:
                selects.append(f"CAST(NULL AS INTEGER) AS c{i}_distinct")

            if column.kind is ColumnKind.NUMERIC:
                selects += [
                    f"MIN({quoted}) AS c{i}_min",
                    f"MAX({quoted}) AS c{i}_max",
                    f"AVG({dialect.cast_double(quoted)}) AS c{i}_mean",
                    f"SUM({dialect.cast_double(quoted)}) AS c{i}_sum",
                    f"SUM(CASE WHEN {quoted} = 0 THEN 1 ELSE 0 END) AS c{i}_zero",
                ]
                selects.append(
                    f"{dialect.stddev(dialect.cast_double(quoted))} AS c{i}_stddev"
                    if dialect.supports_stddev
                    else f"CAST(NULL AS DOUBLE PRECISION) AS c{i}_stddev"
                )
            elif column.kind is ColumnKind.STRING:
                length = dialect.length(quoted)
                trimmed = dialect.trim(dialect.cast_varchar(quoted))
                selects += [
                    f"MIN({quoted}) AS c{i}_min",
                    f"MAX({quoted}) AS c{i}_max",
                    f"MIN({length}) AS c{i}_minlen",
                    f"MAX({length}) AS c{i}_maxlen",
                    f"AVG({dialect.cast_double(length)}) AS c{i}_avglen",
                    f"SUM(CASE WHEN {trimmed} = '' THEN 1 ELSE 0 END) AS c{i}_blank",
                ]
            elif column.kind in (ColumnKind.TEMPORAL, ColumnKind.BOOLEAN):
                selects += [f"MIN({quoted}) AS c{i}_min", f"MAX({quoted}) AS c{i}_max"]

        sql = f"SELECT {', '.join(selects)} FROM {source}{scope}"
        try:
            result = self.connector.execute(sql, max_rows=1)
        except Exception as exc:
            log.warning("Batched profile failed, falling back to per-column: %s", exc)
            return [self._profile_single(source, scope, c, row_count) for c in batch]

        row = dict(zip(result.columns, result.first() or (), strict=False))
        # Key lookup is case-insensitive because Snowflake and Oracle return
        # upper-cased aliases.
        lowered = {k.lower(): v for k, v in row.items()}
        total = int(lowered.get("_total") or row_count or 0)
        return [self._build_profile(column, i, lowered, total) for i, column in enumerate(batch)]

    def _profile_single(
        self, source: str, scope: str, column: ColumnInfo, row_count: int
    ) -> ColumnProfile:
        """Minimal fallback when a batched query is rejected."""
        quoted = self.connector.dialect.quote(column.name)
        sql = (
            f"SELECT COUNT(*) AS _total, COUNT({quoted}) AS c0_nonnull, "
            f"COUNT(DISTINCT {quoted}) AS c0_distinct FROM {source}{scope}"
        )
        try:
            result = self.connector.execute(sql, max_rows=1)
            lowered = {
                k.lower(): v for k, v in zip(result.columns, result.first() or (), strict=False)
            }
            total = int(lowered.get("_total") or row_count or 0)
            return self._build_profile(column, 0, lowered, total)
        except Exception as exc:
            log.debug("Could not profile column %s: %s", column.name, exc)
            return ColumnProfile(
                column=column.name,
                data_type=column.data_type,
                kind=column.kind,
                row_count=row_count,
            )

    @staticmethod
    def _build_profile(
        column: ColumnInfo, index: int, row: dict[str, Any], total: int
    ) -> ColumnProfile:
        def get(suffix: str) -> Any:
            return row.get(f"c{index}_{suffix}")

        def num(suffix: str) -> float | None:
            value = get(suffix)
            try:
                return None if value is None else float(value)
            except (TypeError, ValueError):
                return None

        def integer(suffix: str) -> int | None:
            value = num(suffix)
            return None if value is None else int(value)

        non_null = integer("nonnull") or 0
        null_count = max(total - non_null, 0)
        distinct = integer("distinct")

        profile = ColumnProfile(
            column=column.name,
            data_type=column.data_type,
            kind=column.kind,
            row_count=total,
            null_count=null_count,
            null_ratio=(null_count / total) if total else 0.0,
            completeness=(non_null / total) if total else 1.0,
            distinct_count=distinct,
            min=get("min"),
            max=get("max"),
            mean=num("mean"),
            stddev=num("stddev"),
            sum=num("sum"),
            zero_count=integer("zero"),
            blank_count=integer("blank"),
            min_length=integer("minlen"),
            max_length=integer("maxlen"),
            avg_length=num("avglen"),
        )

        if distinct is not None and non_null:
            profile.distinct_ratio = distinct / non_null
            profile.duplicate_count = non_null - distinct
            # Uniqueness is asserted over non-NULL values, matching how a unique
            # index behaves on most engines.
            profile.is_unique = distinct == non_null
        return profile

    def _add_percentiles(
        self, profile: TableProfile, source: str, scope: str, columns: list[ColumnInfo]
    ) -> None:
        dialect = self.connector.dialect
        for column in columns:
            if column.kind is not ColumnKind.NUMERIC:
                continue
            quoted = dialect.cast_double(dialect.quote(column.name))
            selects = ", ".join(
                f"{dialect.percentile(quoted, fraction)} AS p{label}"
                for label, fraction in (("25", 0.25), ("50", 0.5), ("75", 0.75), ("95", 0.95))
            )
            try:
                result = self.connector.execute(
                    f"SELECT {selects} FROM {source}{scope}", max_rows=1
                )
                values = {
                    k.lower(): v for k, v in zip(result.columns, result.first() or (), strict=False)
                }
            except Exception as exc:
                log.debug("Percentiles unavailable for %s: %s", column.name, exc)
                continue

            target = profile.column_profile(column.name)
            if target is None:
                continue
            for attribute, key in (
                ("p25", "p25"),
                ("median", "p50"),
                ("p75", "p75"),
                ("p95", "p95"),
            ):
                raw = values.get(key)
                if raw is not None:
                    with suppress(TypeError, ValueError):
                        setattr(target, attribute, float(raw))

    def _add_top_values(
        self, profile: TableProfile, source: str, scope: str, columns: list[ColumnInfo]
    ) -> None:
        """Collect the most frequent values for low-cardinality columns.

        Skipped for columns that are unique or near-unique: the top values of a
        primary key are ten arbitrary ids, which tells nobody anything.
        """
        dialect = self.connector.dialect
        limit = self.options.top_values_limit

        for column in columns:
            if column.kind in (ColumnKind.BINARY, ColumnKind.JSON):
                continue
            target = profile.column_profile(column.name)
            if target is None or target.is_unique:
                continue
            if target.distinct_count is not None and target.distinct_count > 1000:
                continue

            quoted = dialect.quote(column.name)
            sql = dialect.limit(
                f"SELECT {quoted} AS value, COUNT(*) AS occurrences FROM {source}{scope} "
                f"GROUP BY {quoted} ORDER BY COUNT(*) DESC",
                limit,
            )
            try:
                result = self.connector.execute(sql, max_rows=limit)
            except Exception as exc:
                log.debug("Top values unavailable for %s: %s", column.name, exc)
                continue

            total = target.row_count or 1
            target.top_values = [
                {
                    "value": None if row[0] is None else str(row[0]),
                    "count": int(row[1] or 0),
                    "ratio": round(int(row[1] or 0) / total, 6),
                }
                for row in result.rows
            ]


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), max(size, 1))]
