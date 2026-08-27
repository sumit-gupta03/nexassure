# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Terminal output.

Everything a human reads in the CLI is rendered here. The guiding rule: a
failure must be actionable from the terminal alone - which check, which table,
which column, what was expected, what was found, and a few offending rows.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..core.enums import CheckStatus, RunStatus
from ..core.models import CheckResult, RunResult, TableProfile

console = Console(stderr=False, soft_wrap=False)
error_console = Console(stderr=True)

_STATUS_STYLE = {
    CheckStatus.PASSED: ("PASS", "bold green"),
    CheckStatus.FAILED: ("FAIL", "bold red"),
    CheckStatus.WARNED: ("WARN", "bold yellow"),
    CheckStatus.ERRORED: ("ERROR", "bold magenta"),
    CheckStatus.SKIPPED: ("SKIP", "dim"),
}

_RUN_STYLE = {
    RunStatus.PASSED: "bold green",
    RunStatus.FAILED: "bold red",
    RunStatus.ERRORED: "bold magenta",
    RunStatus.CANCELLED: "dim",
    RunStatus.RUNNING: "bold cyan",
    RunStatus.PENDING: "dim",
}


def status_text(status: CheckStatus) -> Text:
    label, style = _STATUS_STYLE.get(status, (status.value.upper(), "white"))
    return Text(label, style=style)


def print_result_line(result: CheckResult) -> None:
    """One line per check, printed as results stream in."""
    target = result.dataset or "-"
    if result.column:
        target = f"{target}.{result.column}"
    console.print(
        status_text(result.status),
        Text(f" {result.check_name}", style="bold" if not result.passed else ""),
        Text(f" [{target}]", style="cyan"),
        Text(f" {result.duration_ms:.0f}ms", style="dim"),
        sep="",
    )


def print_run_result(run: RunResult, verbose: bool = False, show_sql: bool = False) -> None:
    """Full report for one run."""
    console.print()
    console.rule(f"[bold]{run.suite_name}[/bold] on [cyan]{run.connection_name}[/cyan]")

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("", width=6)
    table.add_column("Check", overflow="fold")
    table.add_column("Target", style="cyan", overflow="fold")
    table.add_column("Detail", overflow="fold")
    table.add_column("Time", justify="right", style="dim")

    shown = run.results if verbose else [r for r in run.results if not r.passed] or run.results
    for result in shown:
        target = result.dataset or "-"
        if result.column:
            target = f"{target}.{result.column}"
        table.add_row(
            status_text(result.status),
            result.check_name,
            target,
            _detail(result),
            f"{result.duration_ms:.0f}ms",
        )

    console.print(table)

    failures = run.failures()
    if failures:
        console.print()
        for result in failures:
            _print_failure_detail(result, show_sql=show_sql)

    _print_summary(run)


def _detail(result: CheckResult) -> Text:
    if result.status is CheckStatus.PASSED:
        return Text(result.message or "ok", style="dim")
    style = "yellow" if result.status is CheckStatus.WARNED else "red"
    if result.status is CheckStatus.SKIPPED:
        style = "dim"
    return Text(result.message or "", style=style)


def _print_failure_detail(result: CheckResult, show_sql: bool = False) -> None:
    """Expanded panel for a failing check, with evidence."""
    lines: list[str] = []
    if result.description:
        lines.append(f"[bold]Why it matters:[/bold] {result.description}")
    if result.expected is not None:
        lines.append(f"[bold]Expected:[/bold] {result.expected!r}")
    if result.observed is not None:
        lines.append(f"[bold]Observed:[/bold] {result.observed!r}")
    if result.rows_failed is not None and result.rows_scanned:
        ratio = (result.failed_ratio or 0) * 100
        lines.append(
            f"[bold]Rows:[/bold] {result.rows_failed:,} failing of "
            f"{result.rows_scanned:,} scanned ({ratio:.2f}%)"
        )
    if result.owner:
        lines.append(f"[bold]Owner:[/bold] {result.owner}")
    if result.error:
        lines.append(f"[bold red]Error:[/bold red] {result.error}")

    if result.sample_rows:
        lines.append("")
        lines.append("[bold]Sample failing rows:[/bold]")
        lines.append(_render_samples(result.sample_rows))

    if show_sql and result.query:
        lines.append("")
        lines.append("[bold]SQL:[/bold]")
        lines.append(f"[dim]{result.query.strip()}[/dim]")

    border = "magenta" if result.status is CheckStatus.ERRORED else "red"
    console.print(
        Panel(
            "\n".join(lines) or result.message,
            title=f"[bold]{result.check_name}[/bold]  ({result.check_type})",
            border_style=border,
            expand=False,
        )
    )


def _render_samples(rows: list[dict[str, Any]], limit: int = 5) -> str:
    """Render sample rows as an aligned plain-text table.

    Built as text rather than a nested rich Table so it can live inside a Panel
    without fighting the panel width.
    """
    rows = rows[:limit]
    if not rows:
        return ""
    columns = list(rows[0])
    widths = {c: min(max(len(str(c)), *(len(_cell(r.get(c))) for r in rows)), 32) for c in columns}
    header = "  ".join(str(c)[: widths[c]].ljust(widths[c]) for c in columns)
    separator = "  ".join("-" * widths[c] for c in columns)
    body = [
        "  ".join(_cell(row.get(c))[: widths[c]].ljust(widths[c]) for c in columns) for row in rows
    ]
    return "\n".join([f"[dim]{header}[/dim]", f"[dim]{separator}[/dim]", *body])


def _cell(value: Any) -> str:
    return "NULL" if value is None else str(value)


def _print_summary(run: RunResult) -> None:
    s = run.summary
    style = _RUN_STYLE.get(run.status, "white")
    parts = [f"[green]{s.passed} passed[/green]"]
    if s.failed:
        parts.append(f"[red]{s.failed} failed[/red]")
    if s.warned:
        parts.append(f"[yellow]{s.warned} warned[/yellow]")
    if s.errored:
        parts.append(f"[magenta]{s.errored} errored[/magenta]")
    if s.skipped:
        parts.append(f"[dim]{s.skipped} skipped[/dim]")

    console.print()
    console.print(
        f"[{style}]{run.status.value.upper()}[/{style}]  "
        + "  ".join(parts)
        + f"  [dim]({s.total} checks in {run.duration_ms / 1000:.2f}s, "
        f"run {run.run_id[:8]})[/dim]"
    )
    console.print()


def print_run_summaries(runs: list[RunResult]) -> None:
    """Compact roll-up when several suites ran."""
    table = Table(title="Run summary", show_header=True, header_style="bold")
    table.add_column("Suite")
    table.add_column("Connection", style="cyan")
    table.add_column("Status")
    table.add_column("Passed", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Errored", justify="right", style="magenta")
    table.add_column("Time", justify="right", style="dim")

    for run in runs:
        style = _RUN_STYLE.get(run.status, "white")
        table.add_row(
            run.suite_name,
            run.connection_name,
            f"[{style}]{run.status.value}[/{style}]",
            str(run.summary.passed),
            str(run.summary.failed),
            str(run.summary.errored),
            f"{run.duration_ms / 1000:.1f}s",
        )
    console.print(table)


def print_profile(profile: TableProfile, max_columns: int = 60) -> None:
    """Render a table profile."""
    console.print()
    console.rule(f"[bold]{profile.dataset.fqn}[/bold]")

    facts = [
        f"[bold]{profile.row_count:,}[/bold] rows",
        f"[bold]{profile.column_count}[/bold] columns",
    ]
    if profile.duplicate_row_count is not None:
        style = "red" if profile.duplicate_row_count else "green"
        facts.append(f"[{style}]{profile.duplicate_row_count:,}[/{style}] duplicate rows")
    if profile.sampled:
        facts.append(f"[yellow]sampled {profile.sample_size:,}[/yellow]")
    facts.append(f"[dim]{profile.duration_ms / 1000:.2f}s[/dim]")
    console.print("  ".join(facts))
    console.print()

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Column", overflow="fold")
    table.add_column("Type", style="cyan", overflow="fold")
    table.add_column("Nulls", justify="right")
    table.add_column("Distinct", justify="right")
    table.add_column("Dupes", justify="right")
    table.add_column("Min", overflow="fold")
    table.add_column("Max", overflow="fold")
    table.add_column("Mean", justify="right")

    for column in profile.columns[:max_columns]:
        null_style = (
            "red" if column.null_ratio > 0.5 else "yellow" if column.null_ratio else "green"
        )
        distinct = "-" if column.distinct_count is None else f"{column.distinct_count:,}"
        if column.is_unique:
            distinct = f"[green]{distinct} (unique)[/green]"
        dupes = "-" if column.duplicate_count is None else f"{column.duplicate_count:,}"

        table.add_row(
            column.column,
            column.data_type[:24],
            f"[{null_style}]{column.null_count:,} ({column.null_ratio:.1%})[/{null_style}]",
            distinct,
            dupes,
            _truncate(column.min),
            _truncate(column.max),
            "-" if column.mean is None else f"{column.mean:,.3g}",
        )

    console.print(table)
    if len(profile.columns) > max_columns:
        console.print(f"[dim]...and {len(profile.columns) - max_columns} more columns[/dim]")
    console.print()


def _truncate(value: Any, width: int = 18) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def print_error(message: str, hint: str | None = None) -> None:
    error_console.print(f"[bold red]Error:[/bold red] {message}")
    if hint:
        error_console.print(f"[dim]{hint}[/dim]")
