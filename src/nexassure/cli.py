# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""The ``nexassure`` command line interface.

Exit codes are part of the contract, because this runs in CI:

* ``0`` - everything passed (warnings do not fail a build)
* ``1`` - at least one check failed or errored
* ``2`` - NexAssure itself could not run: bad config, unreachable database,
  invalid suite

That separation lets a pipeline tell "the data is bad" apart from "the tool is
broken", which are very different pages to wake someone up for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from .config import write_starter_config
from .exceptions import NexAssureError
from .logging_conf import configure_logging
from .reporting.console import (
    console,
    print_error,
    print_profile,
    print_result_line,
    print_run_result,
    print_run_summaries,
)
from .version import __version__

EXIT_OK = 0
EXIT_CHECKS_FAILED = 1
EXIT_TOOL_ERROR = 2

app = typer.Typer(
    name="nexassure",
    help="Open-source data testing and profiling for the modern warehouse.",
    add_completion=True,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
schedule_app = typer.Typer(help="Manage and run cron schedules.", no_args_is_help=True)
metastore_app = typer.Typer(help="Inspect and maintain the metastore.", no_args_is_help=True)
app.add_typer(schedule_app, name="schedule")
app.add_typer(metastore_app, name="metastore")


# --------------------------------------------------------------------------- #
# Shared options
# --------------------------------------------------------------------------- #

_CONFIG_OPT = typer.Option(
    None, "--config", "-c", help="Path to nexassure.yml (default: search upward from cwd)."
)
_VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="Verbose logging.")


def _open(config: Path | None, verbose: bool = False):
    """Build an :class:`~nexassure.api.NexAssure` instance or exit with code 2."""
    configure_logging("DEBUG" if verbose else "INFO")
    from .api import NexAssure

    try:
        return NexAssure(config)
    except NexAssureError as exc:
        print_error(exc.message, "Run 'nexassure init' to create a project config.")
        raise typer.Exit(EXIT_TOOL_ERROR) from exc


def _emit_json(payload: object) -> None:
    """Write machine-readable JSON to stdout.

    Deliberately bypasses Rich. ``console.print_json`` colourises and re-wraps
    its output, so ``nexassure connectors --json | jq`` would receive ANSI
    escape codes and fail to parse. Anything behind ``--json`` is for a machine,
    so it goes out as plain text on stdout.
    """
    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")


def _fail(exc: Exception, hint: str | None = None) -> None:
    message = exc.message if isinstance(exc, NexAssureError) else str(exc)
    print_error(message, hint)
    raise typer.Exit(EXIT_TOOL_ERROR)


# --------------------------------------------------------------------------- #
# Project setup
# --------------------------------------------------------------------------- #


@app.command()
def init(
    directory: Path = typer.Argument(Path("."), help="Where to create the project."),
    name: str = typer.Option("nexassure", "--name", "-n", help="Project name."),
) -> None:
    """Scaffold nexassure.yml and an example suite."""
    directory = directory.expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        config_path = write_starter_config(directory, name)
    except NexAssureError as exc:
        _fail(exc, "Delete or rename the existing file first.")
        return

    suites_dir = directory / "suites"
    suites_dir.mkdir(exist_ok=True)
    example = suites_dir / "example.yml"
    if not example.exists():
        example.write_text(_EXAMPLE_SUITE, encoding="utf-8")

    console.print(f"[green]Created[/green] {config_path}")
    console.print(f"[green]Created[/green] {example}")
    console.print()
    console.print("Next steps:")
    console.print("  1. Edit [cyan]nexassure.yml[/cyan] and add your connection")
    console.print("  2. [cyan]nexassure test-connection --all[/cyan]")
    console.print("  3. [cyan]nexassure suggest <connection> -o suites/generated.yml[/cyan]")
    console.print("  4. [cyan]nexassure run[/cyan]")


@app.command()
def version() -> None:
    """Show the NexAssure version."""
    console.print(f"nexassure {__version__}")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


@app.command()
def connectors(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List supported databases and whether their drivers are installed."""
    from rich.table import Table

    from .connectors.registry import describe_connectors

    rows = [r for r in describe_connectors() if r.get("canonical")]
    if as_json:
        _emit_json(rows)
        return

    table = Table(title="Connectors", header_style="bold")
    table.add_column("Type")
    table.add_column("Driver", style="cyan")
    table.add_column("Installed")
    table.add_column("Install with", style="dim")

    for row in rows:
        installed = "[green]yes[/green]" if row.get("installed") else "[red]no[/red]"
        table.add_row(
            str(row.get("id")),
            str(row.get("driver_module") or "builtin"),
            installed,
            str(row.get("install_hint") or ""),
        )
    console.print(table)


@app.command("checks")
def list_checks(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List every available check type and its parameters."""
    from rich.table import Table

    from .checks.base import describe_checks

    rows = describe_checks()
    if as_json:
        _emit_json(rows)
        return

    table = Table(title="Check types", header_style="bold")
    table.add_column("Type", style="bold")
    table.add_column("Description", overflow="fold")
    table.add_column("Needs", style="cyan")
    table.add_column("Params", style="dim", overflow="fold")

    for row in rows:
        needs = []
        if row["requires_dataset"]:
            needs.append("dataset")
        if row["requires_column"]:
            needs.append("column")
        table.add_row(
            row["type"], row["summary"], ", ".join(needs) or "-", ", ".join(row["params"]) or "-"
        )
    console.print(table)


@app.command("test-connection")
def test_connection(
    name: str | None = typer.Argument(None, help="Connection name. Omit with --all."),
    all_connections: bool = typer.Option(False, "--all", "-a", help="Test every connection."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Verify credentials and report round-trip latency."""
    na = _open(config, verbose)
    with na:
        if not all_connections and not name:
            print_error("Give a connection name, or pass --all.")
            raise typer.Exit(EXIT_TOOL_ERROR)

        outcomes = na.test_all_connections() if all_connections else [na.test_connection(name)]
        if not outcomes:
            print_error("No connections configured.", "Add one to nexassure.yml.")
            raise typer.Exit(EXIT_TOOL_ERROR)

        failures = 0
        for outcome in outcomes:
            if outcome["ok"]:
                console.print(
                    f"[green]OK[/green]    {outcome['connection']} "
                    f"[dim]({outcome['connector']}, {outcome['latency_ms']}ms"
                    + (f", {outcome['server_version']}" if outcome.get("server_version") else "")
                    + ")[/dim]"
                )
            else:
                failures += 1
                console.print(f"[red]FAIL[/red]  {outcome['connection']}")
                console.print(f"      [dim]{outcome.get('error')}[/dim]")

        if failures:
            raise typer.Exit(EXIT_TOOL_ERROR)


@app.command()
def discover(
    connection: str = typer.Argument(..., help="Connection name."),
    schema: str | None = typer.Option(None, "--schema", "-s", help="Limit to one schema."),
    limit: int = typer.Option(200, "--limit", "-l", help="Maximum tables to catalog."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Walk the catalog and populate the metastore tables."""
    na = _open(config, verbose)
    with na:
        try:
            outcome = na.discover(connection, [schema] if schema else None, max_tables=limit)
        except NexAssureError as exc:
            _fail(exc)
            return

    console.print(
        f"[green]Registered[/green] {outcome['datasets_registered']} dataset(s) "
        f"and {outcome['columns_registered']} column(s) from "
        f"[cyan]{outcome['connection']}[/cyan]"
    )
    console.print(f"[dim]Metastore: {outcome['metastore']}[/dim]")
    if outcome.get("truncated"):
        console.print(
            f"[yellow]Stopped at the {limit}-table limit; raise --limit for more.[/yellow]"
        )


@app.command()
def tables(
    connection: str = typer.Argument(..., help="Connection name."),
    schema: str | None = typer.Option(None, "--schema", "-s"),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """List tables visible on a connection."""
    na = _open(config, verbose)
    with na:
        try:
            for fqn in na.list_tables(connection, schema):
                console.print(fqn)
        except NexAssureError as exc:
            _fail(exc)


# --------------------------------------------------------------------------- #
# Profiling
# --------------------------------------------------------------------------- #


@app.command()
def profile(
    connection: str = typer.Argument(..., help="Connection name."),
    table: str | None = typer.Argument(None, help="Table, e.g. schema.orders."),
    schema: str | None = typer.Option(None, "--schema", "-s", help="Profile a whole schema."),
    limit: int = typer.Option(20, "--limit", "-l", help="Max tables when profiling a schema."),
    sample: int | None = typer.Option(None, "--sample", help="Profile only N rows."),
    percentiles: bool = typer.Option(False, "--percentiles", help="Include median/p25/p75/p95."),
    duplicates: bool = typer.Option(False, "--duplicates", help="Count fully duplicated rows."),
    where: str | None = typer.Option(None, "--where", help="SQL predicate to filter rows."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON here."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Profile a table (or a whole schema) and record the snapshot."""
    from .profiling.profiler import ProfileOptions
    from .reporting.exporters import profile_to_json

    if not table and not schema:
        print_error("Give a table, or pass --schema to profile every table in one.")
        raise typer.Exit(EXIT_TOOL_ERROR)

    options = ProfileOptions(
        include_percentiles=percentiles,
        include_duplicate_rows=duplicates,
        sample_rows=sample,
        where=where,
    )

    na = _open(config, verbose)
    with na:
        try:
            profiles = (
                [na.profile(connection, table, options)]
                if table
                else na.profile_schema(connection, schema, limit, options)
            )
        except NexAssureError as exc:
            _fail(exc)
            return

    for item in profiles:
        print_profile(item)

    if output:
        payload = (
            profile_to_json(profiles[0])
            if len(profiles) == 1
            else json.dumps([p.model_dump(mode="json") for p in profiles], indent=2, default=str)
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        console.print(f"[green]Wrote[/green] {output}")


@app.command()
def suggest(
    connection: str = typer.Argument(..., help="Connection name."),
    table: list[str] = typer.Option([], "--table", "-t", help="Table to profile. Repeatable."),
    schema: str | None = typer.Option(None, "--schema", "-s", help="Profile a whole schema."),
    limit: int = typer.Option(20, "--limit", "-l", help="Max tables when using --schema."),
    suite_name: str = typer.Option("suggested", "--name", help="Name for the generated suite."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write the suite YAML here."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Profile tables and generate a starter suite from what the data shows."""
    from .suites.loader import dump_suite

    na = _open(config, verbose)
    with na:
        try:
            suite = na.suggest(
                connection, list(table) or None, schema, suite_name=suite_name, limit=limit
            )
        except NexAssureError as exc:
            _fail(exc)
            return

    rendered = dump_suite(suite, output)
    if output:
        console.print(f"[green]Wrote[/green] {output} with {len(suite.checks)} suggested check(s).")
        console.print(
            "[dim]Every check is severity 'warn'. Review, tighten, then promote to 'error'.[/dim]"
        )
    else:
        console.print(rendered)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


@app.command()
def validate(
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Lint every suite offline, without connecting to a database."""
    na = _open(config, verbose)
    with na:
        try:
            suites = na.suites()
            problems = na.validate()
        except NexAssureError as exc:
            _fail(exc)
            return

    if not suites:
        print_error("No suites found.", "Check the 'suites:' globs in nexassure.yml.")
        raise typer.Exit(EXIT_TOOL_ERROR)

    if not problems:
        total = sum(len(s.checks) for s in suites)
        console.print(f"[green]Valid[/green] - {len(suites)} suite(s), {total} check(s)")
        return

    for suite_name, issues in problems.items():
        console.print(f"[red]{suite_name}[/red]")
        for issue in issues:
            console.print(f"  - {issue}")
    raise typer.Exit(EXIT_TOOL_ERROR)


@app.command()
def run(
    suite: str | None = typer.Argument(None, help="Suite name. Omit to run every suite."),
    select: list[str] = typer.Option([], "--select", help="Run only these checks. Repeatable."),
    tag: list[str] = typer.Option([], "--tag", help="Run only checks with this tag. Repeatable."),
    dataset: list[str] = typer.Option([], "--dataset", help="Run only checks on this table."),
    max_parallel: int | None = typer.Option(None, "--parallel", "-p", help="Concurrent checks."),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="Stop at the first failure."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan without executing."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write a report file."),
    fmt: str | None = typer.Option(
        None, "--format", "-f", help="json | junit | markdown | html. Inferred from --output."
    ),
    show_sql: bool = typer.Option(False, "--show-sql", help="Print the SQL for failures."),
    verbose_output: bool = typer.Option(False, "--all", help="Show passing checks too."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print the summary."),
    no_notify: bool = typer.Option(False, "--no-notify", help="Skip configured notifications."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Run a suite, or every suite, and report the results."""
    from .reporting.exporters import FORMATTERS, write_report

    na = _open(config, verbose)
    with na:
        progress = None if quiet else print_result_line
        try:
            if suite:
                runs = [
                    na.run_suite(
                        suite,
                        select=list(select) or None,
                        tags=list(tag) or None,
                        datasets=list(dataset) or None,
                        max_parallel=max_parallel,
                        fail_fast=fail_fast,
                        dry_run=dry_run,
                        notify=not no_notify,
                        on_result=progress,
                    )
                ]
            else:
                if not na.suites():
                    print_error("No suites found.", "Check the 'suites:' globs in nexassure.yml.")
                    raise typer.Exit(EXIT_TOOL_ERROR)
                runs = na.run_all(
                    select=list(select) or None,
                    tags=list(tag) or None,
                    datasets=list(dataset) or None,
                    max_parallel=max_parallel,
                    fail_fast=fail_fast,
                    dry_run=dry_run,
                    notify=not no_notify,
                    on_result=progress,
                )
        except NexAssureError as exc:
            _fail(exc)
            return

    for result in runs:
        print_run_result(result, verbose=verbose_output, show_sql=show_sql)
    if len(runs) > 1:
        print_run_summaries(runs)

    if output:
        if fmt and fmt.lower() not in FORMATTERS:
            print_error(f"Unknown format {fmt!r}.", f"Available: {', '.join(sorted(FORMATTERS))}")
            raise typer.Exit(EXIT_TOOL_ERROR)
        # Several runs are merged into the first report file only when there is
        # exactly one; otherwise each gets a suffixed file.
        if len(runs) == 1:
            written = write_report(runs[0], output, fmt)
            console.print(f"[green]Wrote[/green] {written}")
        else:
            for result in runs:
                target = output.with_name(f"{output.stem}.{result.suite_name}{output.suffix}")
                write_report(result, target, fmt)
            console.print(f"[green]Wrote[/green] {len(runs)} report(s) beside {output}")

    if any(r.exit_code for r in runs):
        raise typer.Exit(EXIT_CHECKS_FAILED)


@app.command()
def query(
    connection: str = typer.Argument(..., help="Connection name."),
    sql: str = typer.Argument(..., help="A read-only SQL statement."),
    limit: int = typer.Option(50, "--limit", "-l", help="Max rows to display."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Run an ad-hoc read-only query."""
    from rich.table import Table

    na = _open(config, verbose)
    with na:
        try:
            result = na.query(connection, sql, max_rows=limit)
        except NexAssureError as exc:
            _fail(exc)
            return

    if as_json:
        _emit_json(result.dicts())
        return

    table = Table(header_style="bold", box=None)
    for column in result.columns:
        table.add_column(str(column), overflow="fold")
    for row in result.rows:
        table.add_row(*["NULL" if v is None else str(v) for v in row])
    console.print(table)
    console.print(
        f"[dim]{result.row_count} row(s) in {result.duration_ms:.0f}ms"
        + (" (truncated)" if result.truncated else "")
        + "[/dim]"
    )


@app.command()
def history(
    suite: str | None = typer.Option(None, "--suite", "-s", help="Filter to one suite."),
    limit: int = typer.Option(20, "--limit", "-l"),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Show recent runs from the metastore."""
    from rich.table import Table

    na = _open(config, verbose)
    with na:
        runs = na.history(suite, limit)

    if not runs:
        console.print("[dim]No runs recorded yet.[/dim]")
        return

    table = Table(title="Recent runs", header_style="bold")
    table.add_column("Started", style="dim")
    table.add_column("Suite")
    table.add_column("Status")
    table.add_column("Passed", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Time", justify="right", style="dim")
    table.add_column("Run", style="dim")

    styles = {"passed": "green", "failed": "red", "errored": "magenta"}
    for row in runs:
        status = str(row["status"])
        table.add_row(
            str(row["started_at"])[:19],
            str(row["suite_name"]),
            f"[{styles.get(status, 'white')}]{status}[/{styles.get(status, 'white')}]",
            str(row["passed"]),
            str(row["failed"] + row["errored"]),
            f"{(row['duration_ms'] or 0) / 1000:.1f}s",
            str(row["run_id"])[:8],
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #


@schedule_app.command("list")
def schedule_list(
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """List suites that declare a cron schedule."""
    from rich.table import Table

    from .scheduler.scheduler import describe_schedule

    na = _open(config, verbose)
    with na:
        scheduled = [s for s in na.suites() if s.schedule]

    if not scheduled:
        console.print("[dim]No suite declares a 'schedule:' expression.[/dim]")
        return

    table = Table(title="Schedules", header_style="bold")
    table.add_column("Suite")
    table.add_column("Cron", style="cyan")
    table.add_column("Connection")
    table.add_column("Next runs", style="dim", overflow="fold")

    for suite in scheduled:
        try:
            upcoming = ", ".join(
                t.strftime("%Y-%m-%d %H:%M") for t in describe_schedule(suite.schedule, 2)
            )
        except NexAssureError as exc:
            upcoming = f"[red]{exc.message}[/red]"
        table.add_row(suite.name, suite.schedule, suite.connection, upcoming)
    console.print(table)


@schedule_app.command("run")
def schedule_run(
    workers: int = typer.Option(4, "--workers", "-w", help="Concurrent suite executions."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Start the scheduler in the foreground, firing suites on their cron."""
    from .scheduler.scheduler import Scheduler

    na = _open(config, verbose)
    with na:
        scheduler = Scheduler(
            run_suite=lambda name: na.run_suite(name, triggered_by="schedule"),
            metastore=na.metastore,
            max_workers=workers,
        )
        jobs = scheduler.add_suites(na.suites())
        if not jobs:
            print_error(
                "No suite declares a 'schedule:' expression.",
                "Add e.g.  schedule: '0 6 * * *'  to a suite file.",
            )
            raise typer.Exit(EXIT_TOOL_ERROR)

        console.print(f"[green]Scheduler started[/green] with {len(jobs)} job(s). Ctrl-C to stop.")
        for job in jobs:
            console.print(f"  [cyan]{job.name}[/cyan]  {job.cron}  next {job.next_run_at}")

        try:
            scheduler.start(block=True)
        except KeyboardInterrupt:
            pass
        finally:
            scheduler.stop()
        console.print("[dim]Scheduler stopped.[/dim]")


# --------------------------------------------------------------------------- #
# Metastore
# --------------------------------------------------------------------------- #


@metastore_app.command("info")
def metastore_info(
    hours: int = typer.Option(24, "--hours", "-h", help="Summary window."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Show metastore location and headline numbers."""
    na = _open(config, verbose)
    with na:
        store = na.metastore
        if store is None:
            console.print("[yellow]Metastore is disabled in this project.[/yellow]")
            return
        store.bootstrap()
        console.print(f"[bold]Metastore:[/bold] {store._safe_url()}")
        summary = na.summary(hours)

    for key, value in summary.items():
        if isinstance(value, float):
            value = f"{value:.1%}" if key == "pass_rate" else f"{value:.2f}"
        console.print(f"  {key.replace('_', ' ').title():<24} {value}")


@metastore_app.command("purge")
def metastore_purge(
    days: int = typer.Option(90, "--days", "-d", help="Delete history older than this."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Delete run and profile history older than N days."""
    if not yes:
        typer.confirm(
            f"Permanently delete all runs and profiles older than {days} days?", abort=True
        )

    na = _open(config, verbose)
    with na:
        store = na.metastore
        if store is None:
            console.print("[yellow]Metastore is disabled in this project.[/yellow]")
            return
        removed = store.purge(days)

    if not removed:
        console.print("[dim]Nothing to purge.[/dim]")
        return
    for table, count in removed.items():
        console.print(f"  Removed {count:,} row(s) from {table}")


@metastore_app.command("sync")
def metastore_sync(
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Mirror every suite definition into the metastore check registry."""
    na = _open(config, verbose)
    with na:
        written = na.sync_suites()
    console.print(f"[green]Synced[/green] {written} check definition(s) to the metastore.")


@metastore_app.command("catalog")
def metastore_catalog(
    connection: str | None = typer.Option(None, "--connection", "-c"),
    schema: str | None = typer.Option(None, "--schema", "-s"),
    limit: int = typer.Option(50, "--limit", "-l"),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """List datasets recorded in the metastore."""
    from rich.table import Table

    na = _open(config, verbose)
    with na:
        store = na.metastore
        rows = store.list_datasets(connection, schema, limit) if store else []

    if not rows:
        console.print(
            "[dim]No datasets catalogued yet. Run 'nexassure discover <connection>'.[/dim]"
        )
        return

    table = Table(title="Catalog", header_style="bold")
    table.add_column("Connection", style="cyan")
    table.add_column("Dataset")
    table.add_column("Type", style="dim")
    table.add_column("Columns", justify="right")
    table.add_column("Last seen", style="dim")

    for row in rows:
        table.add_row(
            str(row["connection_name"]),
            str(row["fqn"]),
            str(row["object_type"]),
            str(row["column_count"]),
            str(row["last_seen_at"])[:19],
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# Servers
# --------------------------------------------------------------------------- #


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8080, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes."),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Start the REST API."""
    configure_logging("DEBUG" if verbose else "INFO")
    try:
        import uvicorn
    except ImportError:
        print_error(
            "The REST API needs extra packages.", "Install with: pip install 'nexassure[server]'"
        )
        raise typer.Exit(EXIT_TOOL_ERROR) from None

    from .server.app import create_app

    console.print(f"[green]NexAssure API[/green] on http://{host}:{port}  (docs at /docs)")
    uvicorn.run(create_app(config), host=host, port=port, reload=reload)


@app.command()
def mcp(
    transport: str = typer.Option(
        "stdio", "--transport", "-t", help="stdio (default, for desktop clients), http, or sse."
    ),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8081, "--port", "-p"),
    read_only: bool = typer.Option(
        True,
        "--read-only/--allow-writes",
        help="Read-only blocks tools that mutate files or run non-SELECT SQL.",
    ),
    config: Path | None = _CONFIG_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Start the MCP server so AI agents can profile and test your data."""
    # Logging must go to stderr here: stdio transport owns stdout.
    configure_logging("DEBUG" if verbose else "INFO")
    try:
        from .mcp.server import serve_mcp
    except ImportError:
        print_error(
            "The MCP server needs extra packages.", "Install with: pip install 'nexassure[mcp]'"
        )
        raise typer.Exit(EXIT_TOOL_ERROR) from None

    serve_mcp(config_path=config, transport=transport, host=host, port=port, read_only=read_only)


_EXAMPLE_SUITE = """\
# An example NexAssure suite. Delete this once you have your own.
# Run it with:  nexassure run example
name: example
connection: local
description: Starter checks showing each family of test.

# Fire this suite automatically with 'nexassure schedule run'.
# schedule: "0 6 * * *"

defaults:
  severity: error
  # schema: public

checks:
  # --- Completeness -------------------------------------------------------
  - name: customers_id_not_null
    type: not_null
    description: Every customer row must carry an id, or joins downstream drop rows.
    dataset: customers
    column: id

  # --- Uniqueness ---------------------------------------------------------
  - name: customers_id_unique
    type: unique
    description: The customer id is the join key for the whole warehouse.
    dataset: customers
    column: id

  # --- Volume -------------------------------------------------------------
  - name: customers_has_rows
    type: row_count
    description: A load that leaves this table empty has failed silently.
    dataset: customers
    params:
      min: 1

  # --- Validity -----------------------------------------------------------
  - name: customers_status_is_known
    type: accepted_values
    description: Downstream reporting only understands these three states.
    dataset: customers
    column: status
    params:
      values: [active, churned, trial]
    severity: warn

  # --- Consistency --------------------------------------------------------
  - name: orders_reference_real_customers
    type: referential_integrity
    description: An order with no matching customer breaks revenue attribution.
    dataset: orders
    column: customer_id
    params:
      to: customers
      field: id

  # --- Business rule, expressed as SQL + expected output ------------------
  - name: no_negative_order_totals
    type: custom_sql
    description: Order totals are never negative; a refund belongs in its own table.
    query: |
      SELECT COUNT(*) FROM orders WHERE total < 0
    expect:
      operator: eq
      value: 0
"""


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except NexAssureError as exc:
        print_error(exc.message)
        sys.exit(EXIT_TOOL_ERROR)
    except KeyboardInterrupt:
        print_error("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
