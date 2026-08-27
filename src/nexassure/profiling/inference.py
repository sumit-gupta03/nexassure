# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Turn a profile into a starter suite.

Writing the first fifty checks by hand is the main reason data quality projects
stall. ``nexassure suggest`` profiles a table and proposes the checks the data
itself justifies, which the user then edits down.

The rules are deliberately conservative. A suggested check that fails on the
very next run teaches people to ignore the tool, so each rule below only fires
when the evidence is strong, and bounds are padded rather than fitted tightly
to the sample that happened to be profiled.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.enums import ColumnKind, Severity
from ..core.models import CheckSpec, ColumnProfile, Suite, TableProfile


@dataclass(slots=True)
class InferenceOptions:
    """Thresholds governing which checks get proposed."""

    #: Propose ``not_null`` only when the column is completely populated.
    not_null_requires_zero_nulls: bool = True
    #: Minimum rows before any statistical suggestion is trusted.
    min_rows_for_stats: int = 100
    #: Propose ``accepted_values`` when distinct count is at or below this.
    max_distinct_for_accepted_values: int = 25
    #: ...and the column covers at least this share of rows with those values.
    min_coverage_for_accepted_values: float = 0.99
    #: Pad numeric ranges by this fraction of the observed span.
    range_padding: float = 0.25
    #: Propose a row-count floor at this fraction of the observed count.
    row_count_floor_ratio: float = 0.5
    #: Suggest freshness checks on temporal columns with these name hints.
    freshness_column_hints: tuple[str, ...] = (
        "updated_at",
        "modified_at",
        "created_at",
        "loaded_at",
        "ingested_at",
        "event_time",
        "event_timestamp",
        "_loaded_at",
        "etl_timestamp",
    )
    freshness_max_age_hours: int = 24
    #: Severity for every suggestion. WARN by default so a generated suite
    #: cannot break a pipeline before anyone has reviewed it.
    severity: Severity = Severity.WARN


def suggest_checks(
    profile: TableProfile, options: InferenceOptions | None = None
) -> list[CheckSpec]:
    """Propose checks for one profiled table."""
    opts = options or InferenceOptions()
    fqn = profile.dataset.fqn
    prefix = profile.dataset.table.lower()
    checks: list[CheckSpec] = []

    if profile.row_count:
        floor = max(int(profile.row_count * opts.row_count_floor_ratio), 1)
        checks.append(
            CheckSpec(
                name=f"{prefix}__row_count",
                type="row_count",
                description=(
                    f"{fqn} held {profile.row_count:,} rows when profiled. "
                    f"Alert if volume drops below {floor:,}, which usually means a partial load."
                ),
                dataset=profile.dataset,
                params={"min": floor},
                severity=opts.severity,
                tags=["volume", "auto-suggested"],
            )
        )

    for column in profile.columns:
        checks.extend(_suggest_for_column(profile, column, opts, prefix))

    return checks


def _suggest_for_column(
    profile: TableProfile, column: ColumnProfile, opts: InferenceOptions, prefix: str
) -> list[CheckSpec]:
    suggestions: list[CheckSpec] = []
    name = column.column
    slug = name.lower()
    enough_rows = profile.row_count >= opts.min_rows_for_stats

    def spec(suffix: str, check_type: str, description: str, **kwargs) -> CheckSpec:
        return CheckSpec(
            name=f"{prefix}__{slug}__{suffix}",
            type=check_type,
            description=description,
            dataset=profile.dataset,
            column=name,
            severity=opts.severity,
            tags=[*kwargs.pop("tags", []), "auto-suggested"],
            **kwargs,
        )

    # Completeness: only when the column is currently 100% populated.
    if column.null_count == 0 and column.row_count > 0:
        suggestions.append(
            spec(
                "not_null",
                "not_null",
                f"{name} was fully populated across {column.row_count:,} profiled rows.",
                tags=["completeness"],
            )
        )
    elif enough_rows and 0 < column.null_ratio <= 0.05:
        # Mostly-populated columns get a completeness floor with headroom, so
        # ordinary fluctuation does not trip it.
        floor = round(max(column.completeness - 0.05, 0.0), 4)
        suggestions.append(
            spec(
                "completeness",
                "completeness",
                f"{name} was {column.completeness:.2%} populated when profiled. "
                f"Alert if completeness falls below {floor:.2%}.",
                params={"min_ratio": floor},
                tags=["completeness"],
            )
        )

    # Uniqueness: only for columns that are genuinely unique over enough rows.
    if column.is_unique and enough_rows and column.null_count == 0:
        suggestions.append(
            spec(
                "unique",
                "unique",
                f"{name} held {column.distinct_count:,} distinct values across "
                f"{column.row_count:,} rows with no repeats, so it looks like a key.",
                tags=["uniqueness"],
            )
        )

    # Low-cardinality strings look like enumerations.
    if (
        column.kind in (ColumnKind.STRING, ColumnKind.BOOLEAN)
        and enough_rows
        and column.distinct_count
        and column.distinct_count <= opts.max_distinct_for_accepted_values
        and column.top_values
    ):
        coverage = sum(entry.get("ratio", 0.0) for entry in column.top_values)
        values = [entry["value"] for entry in column.top_values if entry["value"] is not None]
        if coverage >= opts.min_coverage_for_accepted_values and values:
            suggestions.append(
                spec(
                    "accepted_values",
                    "accepted_values",
                    f"{name} only ever held {len(values)} distinct value(s), covering "
                    f"{coverage:.2%} of rows. Treating it as an enumeration.",
                    params={"values": values},
                    tags=["validity"],
                )
            )

    # Numeric bounds, padded so normal drift does not trip them.
    if (
        column.kind is ColumnKind.NUMERIC
        and enough_rows
        and column.min is not None
        and column.max is not None
        and not column.is_unique  # ranges on surrogate keys are meaningless
    ):
        try:
            low, high = float(column.min), float(column.max)
        except (TypeError, ValueError):
            low = high = None
        if low is not None and high is not None:
            span = high - low
            pad = abs(span) * opts.range_padding if span else max(abs(high) * 0.1, 1.0)
            # A column that never went negative probably should not, so keep
            # the floor at zero rather than padding below it.
            padded_low = 0.0 if low >= 0 and low - pad < 0 else round(low - pad, 6)
            suggestions.append(
                spec(
                    "range",
                    "range",
                    f"{name} ranged from {low:,g} to {high:,g} when profiled. "
                    f"Bounds are padded by {opts.range_padding:.0%} of the observed span.",
                    params={"min": padded_low, "max": round(high + pad, 6)},
                    tags=["validity"],
                )
            )

    # Freshness, only for timestamp columns whose name says they track loading.
    if column.kind is ColumnKind.TEMPORAL and slug in opts.freshness_column_hints:
        suggestions.append(
            spec(
                "freshness",
                "freshness",
                f"{name} looks like a load or update timestamp. "
                f"Alert when the newest row is more than {opts.freshness_max_age_hours}h old.",
                params={"max_age_hours": opts.freshness_max_age_hours},
                tags=["timeliness"],
            )
        )

    return suggestions


def suggest_suite(
    profiles: list[TableProfile],
    connection_name: str,
    suite_name: str = "suggested",
    options: InferenceOptions | None = None,
) -> Suite:
    """Build a complete, ready-to-edit suite from one or more table profiles."""
    checks: list[CheckSpec] = []
    for profile in profiles:
        checks.extend(suggest_checks(profile, options))

    tables = ", ".join(p.dataset.fqn for p in profiles[:5])
    if len(profiles) > 5:
        tables += f" and {len(profiles) - 5} more"

    return Suite(
        name=suite_name,
        connection=connection_name,
        description=(
            f"Auto-generated from profiling {tables}. "
            "Every check is severity 'warn' and tagged 'auto-suggested' - review, "
            "tighten and promote the ones that reflect real contracts, and delete the rest."
        ),
        checks=checks,
        tags=["auto-suggested"],
    )
