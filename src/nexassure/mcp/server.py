# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Model Context Protocol server.

Exposes NexAssure to AI agents so they can explore a warehouse, profile it, write
checks and run them - the loop a data engineer performs by hand.

Design decisions that matter for agent use:

* **Read-only by default.** Every SQL path goes through the same guard the CLI
  uses. An agent cannot drop a table through this server.
* **Bounded results.** Tools cap rows and truncate long values. An agent that
  asks to profile a 500-column table should not blow its context window.
* **Errors are values.** A failed tool returns ``{"ok": false, "error": ...}``
  rather than raising, so the agent can read the reason and adapt instead of
  seeing an opaque protocol error.
* **Descriptions are the API.** Each tool docstring is what the model actually
  reads, so they state when to use the tool, not just what it does.

Run it with ``nexassure mcp``, or wire it into a client config:

.. code-block:: json

    {
      "mcpServers": {
        "nexassure": {
          "command": "nexassure",
          "args": ["mcp", "--config", "/path/to/nexassure.yml"]
        }
      }
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging_conf import get_logger
from ..version import __version__

log = get_logger(__name__)

#: Hard caps, so a single tool call cannot flood an agent context window.
MAX_QUERY_ROWS = 200
MAX_TABLES_LISTED = 500
MAX_PROFILE_COLUMNS = 200
MAX_VALUE_CHARS = 500

INSTRUCTIONS = """\
NexAssure gives you read-only access to data warehouses plus a data testing engine.

A productive loop looks like:
  1. list_connections            - see what databases are configured
  2. list_tables / describe_table - explore the catalog
  3. profile_table               - learn the actual shape of the data
  4. suggest_checks              - get a starter suite grounded in that profile
  5. run_check / run_suite       - execute tests and read the failures

Prefer profile_table over ad-hoc COUNT queries: it is one batched pass and
returns nulls, cardinality, ranges and top values together.

run_query is read-only. Write statements are rejected, so use it freely to
investigate, and use run_check when you want a pass/fail verdict recorded.
"""


def _truncate(value: Any, limit: int = MAX_VALUE_CHARS) -> Any:
    """Shorten long values so tool output stays readable."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... [{len(value) - limit} more chars]"
    return value


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _err(exc: Exception, **context: Any) -> dict[str, Any]:
    """Render an exception as a value the agent can reason about."""
    from ..exceptions import NexAssureError

    if isinstance(exc, NexAssureError):
        return {"ok": False, "error": exc.message, "code": exc.code, **exc.context, **context}
    return {"ok": False, "error": str(exc), "code": type(exc).__name__, **context}


def load_server_class() -> tuple[type, int]:
    """Resolve the MCP server class across SDK generations.

    The Python MCP SDK renamed ``FastMCP`` to ``MCPServer`` in 2.0. The
    decorator surface NexAssure relies on - ``tool()``, ``resource()``, ``run()``,
    ``call_tool()`` - is the same on both, so supporting each is a small import
    shim rather than a fork. Returns ``(class, major_version)``.
    """
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2.0

        return MCPServer, 2
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP, 1
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(
            "The MCP server needs the 'mcp' package. Install with: pip install 'nexassure[mcp]'"
        ) from exc


def build_server(config_path: str | Path | None = None, read_only: bool = True) -> Any:
    """Construct the MCP server with every tool registered.

    Args:
        config_path: Path to ``nexassure.yml``. Defaults to the usual upward search.
        read_only: When true, tools that write files are not registered.
    """
    server_class, _sdk_major = load_server_class()

    from ..api import NexAssure

    mcp = server_class("nexassure", instructions=INSTRUCTIONS)
    # One long-lived instance keeps connection pools warm across tool calls,
    # which matters because agents make many small calls in a row.
    state: dict[str, NexAssure] = {}

    def na() -> NexAssure:
        if "instance" not in state:
            state["instance"] = NexAssure(config_path)
        return state["instance"]

    # ---------------------------------------------------------------- meta --

    @mcp.tool()
    def nexassure_info() -> dict[str, Any]:
        """Report the NexAssure version, project name, and which features are available.

        Call this first if you are unsure whether a project config was loaded.
        """
        try:
            instance = na()
            from ..checks.base import available_checks
            from ..connectors.registry import describe_connectors

            installed = [
                r["id"] for r in describe_connectors() if r.get("canonical") and r.get("installed")
            ]
            return _ok(
                version=__version__,
                project=instance.config.project,
                config_file=str(instance.config.root) if instance.config.root else None,
                connections=[c.name for c in instance.config.connections],
                suites=[s.name for s in instance.suites()],
                installed_connectors=installed,
                check_types=available_checks(),
                read_only=read_only,
                metastore_enabled=instance.config.metastore.enabled,
            )
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def list_check_types() -> dict[str, Any]:
        """List every check type with its parameters.

        Use this before writing a suite so you use real type names and real
        parameter names rather than guessing.
        """
        try:
            from ..checks.base import describe_checks

            return _ok(check_types=describe_checks())
        except Exception as exc:
            return _err(exc)

    # --------------------------------------------------------- connections --

    @mcp.tool()
    def list_connections() -> dict[str, Any]:
        """List configured database connections, with credentials redacted.

        Start here: every other tool takes a connection name from this list.
        """
        try:
            return _ok(connections=na().list_connections())
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def test_connection(connection: str) -> dict[str, Any]:
        """Check that a connection works and report its latency and server version.

        Args:
            connection: Name from list_connections.
        """
        try:
            return _ok(**na().test_connection(connection))
        except Exception as exc:
            return _err(exc, connection=connection)

    # ------------------------------------------------------------- catalog --

    @mcp.tool()
    def list_schemas(connection: str) -> dict[str, Any]:
        """List the schemas visible on a connection.

        Args:
            connection: Name from list_connections.
        """
        try:
            return _ok(connection=connection, schemas=na().list_schemas(connection))
        except Exception as exc:
            return _err(exc, connection=connection)

    @mcp.tool()
    def list_tables(connection: str, schema: str | None = None) -> dict[str, Any]:
        """List tables and views, returning fully-qualified names.

        Args:
            connection: Name from list_connections.
            schema: Restrict to one schema. Omit to use the connection default.
        """
        try:
            found = na().list_tables(connection, schema)
            return _ok(
                connection=connection,
                schema=schema,
                table_count=len(found),
                tables=found[:MAX_TABLES_LISTED],
                truncated=len(found) > MAX_TABLES_LISTED,
            )
        except Exception as exc:
            return _err(exc, connection=connection, schema=schema)

    @mcp.tool()
    def describe_table(connection: str, table: str) -> dict[str, Any]:
        """Get the columns, types and nullability of a table.

        Args:
            connection: Name from list_connections.
            table: Table name, optionally qualified as schema.table or db.schema.table.
        """
        try:
            dataset = na().describe_table(connection, table)
            return _ok(connection=connection, dataset=dataset)
        except Exception as exc:
            return _err(exc, connection=connection, table=table)

    @mcp.tool()
    def discover_catalog(
        connection: str, schema: str | None = None, max_tables: int = 100
    ) -> dict[str, Any]:
        """Walk the catalog and record every table and column in the NexAssure metastore.

        Use this once per connection so later tools can answer questions about
        the catalog without re-introspecting the warehouse.

        Args:
            connection: Name from list_connections.
            schema: Restrict discovery to one schema.
            max_tables: Stop after this many tables.
        """
        try:
            outcome = na().discover(connection, [schema] if schema else None, max_tables)
            return _ok(**outcome)
        except Exception as exc:
            return _err(exc, connection=connection, schema=schema)

    # ----------------------------------------------------------- profiling --

    @mcp.tool()
    def profile_table(
        connection: str,
        table: str,
        sample_rows: int | None = None,
        include_percentiles: bool = False,
        include_duplicate_rows: bool = False,
        where: str | None = None,
    ) -> dict[str, Any]:
        """Profile a table: row count, nulls, cardinality, ranges, lengths and top values.

        This is the highest-value tool for understanding unfamiliar data. It runs
        as a small number of batched queries, so prefer it over issuing many
        separate COUNT or MIN/MAX queries yourself.

        Args:
            connection: Name from list_connections.
            table: Table name, optionally schema-qualified.
            sample_rows: Profile only this many rows. Use on very large tables.
            include_percentiles: Also compute median, p25, p75, p95 for numeric columns.
            include_duplicate_rows: Also count fully duplicated rows. Expensive.
            where: SQL predicate restricting which rows are profiled.
        """
        try:
            from ..profiling.profiler import ProfileOptions

            profile = na().profile(
                connection,
                table,
                ProfileOptions(
                    sample_rows=sample_rows,
                    include_percentiles=include_percentiles,
                    include_duplicate_rows=include_duplicate_rows,
                    where=where,
                ),
            )
            payload = profile.model_dump(mode="json")
            columns = payload.get("columns", [])
            payload["columns"] = columns[:MAX_PROFILE_COLUMNS]
            payload["columns_truncated"] = len(columns) > MAX_PROFILE_COLUMNS
            return _ok(profile=payload)
        except Exception as exc:
            return _err(exc, connection=connection, table=table)

    @mcp.tool()
    def suggest_checks(
        connection: str,
        tables: list[str] | None = None,
        schema: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Profile tables and propose a data quality suite grounded in what the data shows.

        Returns suite YAML you can review, edit and save. Suggestions are
        conservative and all severity 'warn' - tighten them before relying on
        them to gate a pipeline.

        Args:
            connection: Name from list_connections.
            tables: Specific tables to profile. Omit to sweep a schema.
            schema: Schema to sweep when tables is omitted.
            limit: Maximum tables to profile when sweeping.
        """
        try:
            from ..suites.loader import dump_suite

            suite = na().suggest(connection, tables, schema, limit=limit)
            return _ok(
                suite_name=suite.name,
                check_count=len(suite.checks),
                yaml=dump_suite(suite),
                checks=[
                    {
                        "name": c.name,
                        "type": c.type,
                        "dataset": c.dataset.fqn if c.dataset else None,
                        "column": c.column,
                        "description": c.description,
                        "params": c.params,
                    }
                    for c in suite.checks
                ],
            )
        except Exception as exc:
            return _err(exc, connection=connection)

    # ------------------------------------------------------------- testing --

    @mcp.tool()
    def list_suites() -> dict[str, Any]:
        """List the check suites defined in this project, with their check counts."""
        try:
            return _ok(
                suites=[
                    {
                        "name": s.name,
                        "connection": s.connection,
                        "description": s.description,
                        "check_count": len(s.checks),
                        "schedule": s.schedule,
                        "tags": s.tags,
                        "source": s.source_path,
                    }
                    for s in na().suites()
                ]
            )
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def validate_suites() -> dict[str, Any]:
        """Lint every suite offline and report problems, without touching a database.

        Call this after editing suite files to catch unknown check types,
        missing required parameters and dependency cycles.
        """
        try:
            problems = na().validate()
            return _ok(valid=not problems, problems=problems)
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def run_suite(
        suite: str,
        select: list[str] | None = None,
        tags: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run a check suite and return every result, with failure evidence.

        Args:
            suite: Suite name from list_suites.
            select: Run only these check names.
            tags: Run only checks carrying one of these tags.
            dry_run: Report the plan without executing anything.
        """
        try:
            run = na().run_suite(
                suite,
                select=select,
                tags=tags,
                dry_run=dry_run,
                triggered_by="mcp",
                notify=False,
            )
            return _ok(**_render_run(run))
        except Exception as exc:
            return _err(exc, suite=suite)

    @mcp.tool()
    def run_check(
        connection: str,
        check_type: str,
        name: str = "adhoc_check",
        dataset: str | None = None,
        column: str | None = None,
        query: str | None = None,
        params: dict[str, Any] | None = None,
        expect: dict[str, Any] | None = None,
        where: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Run a single check immediately, without saving it to a suite file.

        Use this to test a hypothesis about the data. Call list_check_types
        first to see the valid check_type values and their parameters.

        For a custom rule, set check_type to 'custom_sql', put your SELECT in
        query, and describe the expected answer in expect, for example
        {"operator": "eq", "value": 0}.

        Args:
            connection: Name from list_connections.
            check_type: A type from list_check_types, e.g. not_null, unique, custom_sql.
            name: Label for the result.
            dataset: Target table for built-in checks.
            column: Target column, where the check type needs one.
            query: SQL for custom_sql and sql_returns_no_rows.
            params: Check-type specific options, e.g. {"values": ["a", "b"]}.
            expect: Expectation for custom_sql, e.g. {"operator": "lte", "value": 100}.
            where: SQL predicate restricting the rows considered.
            description: What the rule means, echoed back in the result.
        """
        try:
            from ..core.models import CheckSpec

            spec = CheckSpec(
                name=name,
                type=check_type,
                description=description,
                dataset=dataset,
                column=column,
                query=query,
                params=params or {},
                expect=expect,
                where=where,
            )
            result = na().run_check(connection, spec)
            return _ok(result=_render_result(result))
        except Exception as exc:
            return _err(exc, connection=connection, check_type=check_type)

    @mcp.tool()
    def run_query(connection: str, sql: str, max_rows: int = 50) -> dict[str, Any]:
        """Run a read-only SQL query and return the rows.

        Write statements (INSERT, UPDATE, DELETE, DROP, ...) are rejected, so
        this is safe to use for open-ended investigation. Results are capped;
        aggregate in SQL rather than pulling large result sets.

        Args:
            connection: Name from list_connections.
            sql: A single SELECT (or WITH / SHOW / DESCRIBE) statement.
            max_rows: Rows to return, capped at 200.
        """
        try:
            capped = max(1, min(max_rows, MAX_QUERY_ROWS))
            result = na().query(connection, sql, max_rows=capped)
            rows = [{k: _truncate(v) for k, v in row.items()} for row in result.dicts()]
            return _ok(
                columns=result.columns,
                row_count=result.row_count,
                rows=rows,
                truncated=result.truncated,
                duration_ms=result.duration_ms,
            )
        except Exception as exc:
            return _err(exc, connection=connection)

    # ------------------------------------------------------------- history --

    @mcp.tool()
    def quality_summary(hours: int = 24) -> dict[str, Any]:
        """Headline data quality numbers over a recent window.

        Answers "how is data quality doing right now" in one call.

        Args:
            hours: Size of the window to summarise.
        """
        try:
            return _ok(summary=na().summary(hours))
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def recent_failures(hours: int = 24, limit: int = 50) -> dict[str, Any]:
        """List checks that failed or errored recently, newest first.

        Use this to triage: it returns the check, the table, the message and
        the observed value for each failure.

        Args:
            hours: How far back to look.
            limit: Maximum failures to return.
        """
        try:
            store = na().metastore
            if store is None:
                return _ok(failures=[], note="The metastore is disabled in this project.")
            rows = store.failing_checks(hours, limit)
            return _ok(
                failure_count=len(rows),
                failures=[
                    {
                        "check": r["check_name"],
                        "type": r["check_type"],
                        "suite": r["suite_name"],
                        "status": r["status"],
                        "severity": r["severity"],
                        "dataset": r["dataset_fqn"],
                        "column": r["column_name"],
                        "message": _truncate(r["message"]),
                        "rows_failed": r["rows_failed"],
                        "started_at": str(r["started_at"]),
                    }
                    for r in rows
                ],
            )
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def run_history(suite: str | None = None, limit: int = 20) -> dict[str, Any]:
        """List recent suite runs and their outcomes.

        Args:
            suite: Restrict to one suite.
            limit: Maximum runs to return.
        """
        try:
            runs = na().history(suite, limit)
            return _ok(
                runs=[
                    {
                        "run_id": r["run_id"],
                        "suite": r["suite_name"],
                        "connection": r["connection_name"],
                        "status": r["status"],
                        "passed": r["passed"],
                        "failed": r["failed"],
                        "errored": r["errored"],
                        "pass_rate": r["pass_rate"],
                        "started_at": str(r["started_at"]),
                        "duration_ms": r["duration_ms"],
                    }
                    for r in runs
                ]
            )
        except Exception as exc:
            return _err(exc, suite=suite)

    @mcp.tool()
    def get_run(run_id: str) -> dict[str, Any]:
        """Fetch one recorded run with all of its check results.

        Args:
            run_id: Identifier from run_history or a run_suite response.
        """
        try:
            run = na().get_run(run_id)
            if run is None:
                return {"ok": False, "error": f"No run recorded with id {run_id!r}"}
            return _ok(run=json.loads(json.dumps(run, default=str)))
        except Exception as exc:
            return _err(exc, run_id=run_id)

    # ------------------------------------------------------- write actions --

    if not read_only:

        @mcp.tool()
        def save_suite(path: str, yaml_content: str) -> dict[str, Any]:
            """Write suite YAML to a file, then validate it.

            Only available when the server was started with --allow-writes.

            Args:
                path: Destination file path, relative to the project directory.
                yaml_content: The suite YAML, e.g. from suggest_checks.
            """
            try:
                from ..suites.loader import load_suite_file, validate_suite

                instance = na()
                target = Path(path).expanduser()
                if not target.is_absolute():
                    target = instance.config.project_dir / target
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(yaml_content, encoding="utf-8")

                problems: list[str] = []
                for suite in load_suite_file(target):
                    problems.extend(validate_suite(suite))
                instance.suites(reload=True)

                return _ok(path=str(target), valid=not problems, problems=problems)
            except Exception as exc:
                return _err(exc, path=path)

    # ----------------------------------------------------------- resources --

    @mcp.resource("nexassure://catalog")
    def catalog_resource() -> str:
        """Every dataset NexAssure has catalogued, as JSON."""
        try:
            store = na().metastore
            rows = store.list_datasets(limit=1000) if store else []
            return json.dumps(rows, indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    @mcp.resource("nexassure://checks")
    def checks_resource() -> str:
        """The catalog of available check types, as JSON."""
        from ..checks.base import describe_checks

        return json.dumps(describe_checks(), indent=2)

    return mcp


def _render_run(run: Any) -> dict[str, Any]:
    """Flatten a RunResult into agent-friendly JSON."""
    return {
        "run_id": run.run_id,
        "suite": run.suite_name,
        "connection": run.connection_name,
        "status": run.status.value,
        "summary": run.summary.model_dump(),
        "pass_rate": round(run.summary.pass_rate, 4),
        "duration_ms": run.duration_ms,
        "results": [_render_result(r) for r in run.results],
    }


def _render_result(result: Any) -> dict[str, Any]:
    return {
        "check": result.check_name,
        "type": result.check_type,
        "status": result.status.value,
        "severity": result.severity.value,
        "description": result.description,
        "dataset": result.dataset,
        "column": result.column,
        "message": _truncate(result.message),
        "observed": _truncate(result.observed)
        if isinstance(result.observed, str)
        else result.observed,
        "expected": result.expected,
        "rows_scanned": result.rows_scanned,
        "rows_failed": result.rows_failed,
        "sample_rows": result.sample_rows[:5],
        "query": _truncate(result.query, 1000),
        "duration_ms": result.duration_ms,
        "error": result.error,
    }


#: Transports accepted on the command line, mapped to SDK transport names.
TRANSPORTS = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable-http",
    "streamable-http": "streamable-http",
}


def serve_mcp(
    config_path: str | Path | None = None,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8081,
    read_only: bool = True,
) -> None:
    """Start the MCP server.

    Args:
        config_path: Path to ``nexassure.yml``.
        transport: ``stdio`` for local clients (the default, and what desktop
            MCP clients expect), ``http`` for a networked server, or the older
            ``sse``.
        host: Bind address for the network transports.
        port: Port for the network transports.
        read_only: Withhold tools that write files.
    """
    if transport not in TRANSPORTS:
        raise ValueError(
            f"Unknown transport {transport!r}. Use one of: {', '.join(sorted(TRANSPORTS))}"
        )

    server = build_server(config_path, read_only)
    resolved = TRANSPORTS[transport]
    log.info("Starting NexAssure MCP server (%s, read_only=%s)", resolved, read_only)

    if resolved == "stdio":
        server.run(transport="stdio")
        return

    if resolved == "streamable-http":
        try:
            server.run(transport="streamable-http", host=host, port=port)
            return
        except (TypeError, ValueError):
            # mcp 1.x has no streamable-http transport; fall back to SSE.
            log.info("streamable-http is unavailable in this SDK, falling back to sse")
            resolved = "sse"

    # mcp 1.x reads the bind address off a settings object; 2.x takes kwargs.
    settings = getattr(server, "settings", None)
    if settings is not None:
        settings.host = host
        settings.port = port
        server.run(transport="sse")
    else:
        server.run(transport="sse", host=host, port=port)


def main() -> None:
    """Entry point for the ``nexassure-mcp`` console script."""
    import argparse

    from ..logging_conf import configure_logging

    parser = argparse.ArgumentParser(description="NexAssure MCP server")
    parser.add_argument("--config", help="Path to nexassure.yml")
    parser.add_argument("--transport", default="stdio", choices=sorted(TRANSPORTS))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--allow-writes", action="store_true", help="Enable file-writing tools")
    args = parser.parse_args()

    # stdout belongs to the protocol; logs go to stderr.
    configure_logging("INFO")
    serve_mcp(args.config, args.transport, args.host, args.port, not args.allow_writes)


if __name__ == "__main__":
    main()
