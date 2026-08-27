# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Optional REST API.

Gives dashboards, orchestrators and internal tools an HTTP surface over the
same facade the CLI uses. Install with ``pip install 'nexassure[server]'`` and
start it with ``nexassure serve``.

The API is unauthenticated by design - it is meant to run inside a trusted
network or behind a gateway that owns auth. Setting ``NEXASSURE_API_TOKEN`` turns
on a simple bearer-token check for cases where that is not possible.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..api import NexAssure
from ..exceptions import NexAssureError
from ..logging_conf import get_logger
from ..version import __version__

log = get_logger(__name__)


class RunRequest(BaseModel):
    """Body for ``POST /suites/{name}/run``."""

    select: list[str] | None = Field(None, description="Only run these check names")
    tags: list[str] | None = Field(None, description="Only run checks with these tags")
    datasets: list[str] | None = Field(None, description="Only run checks on these tables")
    max_parallel: int | None = Field(None, ge=1, le=64)
    fail_fast: bool = False
    dry_run: bool = False
    notify: bool = True


class QueryRequest(BaseModel):
    """Body for ``POST /connections/{name}/query``."""

    sql: str = Field(..., description="A single read-only statement")
    max_rows: int = Field(100, ge=1, le=10_000)


class CheckRequest(BaseModel):
    """Body for ``POST /connections/{name}/check`` - run one ad-hoc check."""

    type: str
    name: str = "adhoc_check"
    dataset: str | None = None
    column: str | None = None
    query: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] | None = None
    where: str | None = None
    description: str | None = None


def create_app(config_path: str | Path | None = None) -> FastAPI:
    """Build the FastAPI application."""
    # One instance for the app lifetime keeps connection pools and the
    # metastore engine warm between requests.
    state: dict[str, NexAssure] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Dispose of connection pools when the server stops."""
        yield
        instance = state.pop("instance", None)
        if instance is not None:
            instance.close()

    app = FastAPI(
        title="NexAssure",
        version=__version__,
        description=(
            "Data testing, profiling and observability for the modern warehouse. "
            "See https://github.com/sumit-gupta03/nexassure"
        ),
        lifespan=lifespan,
    )

    def get_nexassure() -> NexAssure:
        if "instance" not in state:
            state["instance"] = NexAssure(config_path)
        return state["instance"]

    def require_token(request: Request) -> None:
        """Bearer-token gate, active only when ``NEXASSURE_API_TOKEN`` is set."""
        expected = os.getenv("NEXASSURE_API_TOKEN")
        if not expected:
            return
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        # Constant-time compare, so the token cannot be recovered by timing.
        import secrets

        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Invalid or missing bearer token")

    guarded = [Depends(require_token)]

    @app.exception_handler(NexAssureError)
    async def _nexassure_error(_request: Request, exc: NexAssureError):
        from fastapi.responses import JSONResponse

        status = 404 if exc.code in ("unknown_connector", "suite_error", "config_error") else 400
        return JSONResponse(status_code=status, content=exc.to_dict())

    # ------------------------------------------------------------ health --

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, Any]:
        """Liveness probe."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready", tags=["meta"])
    def ready(na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """Readiness probe: config parsed and metastore reachable."""
        store = na.metastore
        metastore_ok = True if store is None else store.bootstrap()
        return {
            "status": "ok" if metastore_ok else "degraded",
            "project": na.config.project,
            "connections": len(na.config.connections),
            "suites": len(na.suites()),
            "metastore": "ok" if metastore_ok else "unavailable",
        }

    @app.get("/info", tags=["meta"], dependencies=guarded)
    def info(na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """Project overview: connectors installed, check types, suites loaded."""
        from ..checks.base import available_checks
        from ..connectors.registry import describe_connectors

        return {
            "version": __version__,
            "project": na.config.project,
            "config_file": str(na.config.root) if na.config.root else None,
            "connectors": [r for r in describe_connectors() if r.get("canonical")],
            "check_types": available_checks(),
            "suites": [s.name for s in na.suites()],
        }

    # ------------------------------------------------------- connections --

    @app.get("/connections", tags=["connections"], dependencies=guarded)
    def list_connections(na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """List configured connections, credentials redacted."""
        return {"connections": na.list_connections()}

    @app.get("/connections/{name}/test", tags=["connections"], dependencies=guarded)
    def test_connection(name: str, na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """Verify a connection and report latency."""
        return na.test_connection(name)

    @app.get("/connections/{name}/schemas", tags=["catalog"], dependencies=guarded)
    def list_schemas(name: str, na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """List schemas on a connection."""
        return {"connection": name, "schemas": na.list_schemas(name)}

    @app.get("/connections/{name}/tables", tags=["catalog"], dependencies=guarded)
    def list_tables(
        name: str,
        schema: str | None = Query(None),
        na: NexAssure = Depends(get_nexassure),
    ) -> dict[str, Any]:
        """List tables and views on a connection."""
        return {"connection": name, "schema": schema, "tables": na.list_tables(name, schema)}

    @app.get("/connections/{name}/tables/{table}", tags=["catalog"], dependencies=guarded)
    def describe_table(
        name: str, table: str, na: NexAssure = Depends(get_nexassure)
    ) -> dict[str, Any]:
        """Describe a table: columns, types, nullability, keys."""
        return na.describe_table(name, table)

    @app.post("/connections/{name}/discover", tags=["catalog"], dependencies=guarded)
    def discover(
        name: str,
        schema: str | None = Query(None),
        max_tables: int = Query(200, ge=1, le=5000),
        na: NexAssure = Depends(get_nexassure),
    ) -> dict[str, Any]:
        """Catalog a connection into the metastore."""
        return na.discover(name, [schema] if schema else None, max_tables)

    @app.post("/connections/{name}/query", tags=["query"], dependencies=guarded)
    def run_query(
        name: str, body: QueryRequest, na: NexAssure = Depends(get_nexassure)
    ) -> dict[str, Any]:
        """Run a read-only query. Write statements are rejected."""
        result = na.query(name, body.sql, max_rows=body.max_rows)
        return {
            "columns": result.columns,
            "rows": result.dicts(),
            "row_count": result.row_count,
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
        }

    @app.post("/connections/{name}/check", tags=["checks"], dependencies=guarded)
    def run_adhoc_check(
        name: str, body: CheckRequest, na: NexAssure = Depends(get_nexassure)
    ) -> dict[str, Any]:
        """Run a single check without saving it to a suite."""
        from ..core.models import CheckSpec

        spec = CheckSpec(
            name=body.name,
            type=body.type,
            description=body.description,
            dataset=body.dataset,
            column=body.column,
            query=body.query,
            params=body.params,
            expect=body.expect,
            where=body.where,
        )
        return na.run_check(name, spec).model_dump(mode="json")

    # --------------------------------------------------------- profiling --

    @app.post("/connections/{name}/profile", tags=["profiling"], dependencies=guarded)
    def profile_table(
        name: str,
        table: str = Query(..., description="Table, optionally schema-qualified"),
        sample_rows: int | None = Query(None, ge=1),
        include_percentiles: bool = Query(False),
        include_duplicate_rows: bool = Query(False),
        na: NexAssure = Depends(get_nexassure),
    ) -> dict[str, Any]:
        """Profile a table and record the snapshot."""
        from ..profiling.profiler import ProfileOptions

        profile = na.profile(
            name,
            table,
            ProfileOptions(
                sample_rows=sample_rows,
                include_percentiles=include_percentiles,
                include_duplicate_rows=include_duplicate_rows,
            ),
        )
        return profile.model_dump(mode="json")

    @app.post("/connections/{name}/suggest", tags=["profiling"], dependencies=guarded)
    def suggest(
        name: str,
        schema: str | None = Query(None),
        limit: int = Query(10, ge=1, le=200),
        na: NexAssure = Depends(get_nexassure),
    ) -> dict[str, Any]:
        """Profile tables and return a suggested suite."""
        from ..suites.loader import dump_suite

        suite = na.suggest(name, None, schema, limit=limit)
        return {"suite": suite.model_dump(mode="json"), "yaml": dump_suite(suite)}

    # ------------------------------------------------------------ suites --

    @app.get("/suites", tags=["suites"], dependencies=guarded)
    def list_suites(na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """List loaded suites."""
        return {
            "suites": [
                {
                    "name": s.name,
                    "connection": s.connection,
                    "description": s.description,
                    "check_count": len(s.checks),
                    "schedule": s.schedule,
                    "tags": s.tags,
                }
                for s in na.suites()
            ]
        }

    @app.get("/suites/{name}", tags=["suites"], dependencies=guarded)
    def get_suite(name: str, na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """Fetch one suite definition."""
        return na.suite(name).model_dump(mode="json")

    @app.get("/suites/validate", tags=["suites"], dependencies=guarded)
    def validate_suites(na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """Lint every suite offline."""
        problems = na.validate()
        return {"valid": not problems, "problems": problems}

    @app.post("/suites/{name}/run", tags=["runs"], dependencies=guarded)
    def run_suite(
        name: str, body: RunRequest | None = None, na: NexAssure = Depends(get_nexassure)
    ) -> dict[str, Any]:
        """Run a suite synchronously and return the full result."""
        options = body or RunRequest()
        run = na.run_suite(
            name,
            select=options.select,
            tags=options.tags,
            datasets=options.datasets,
            max_parallel=options.max_parallel,
            fail_fast=options.fail_fast,
            dry_run=options.dry_run,
            notify=options.notify,
            triggered_by="api",
        )
        return run.model_dump(mode="json")

    @app.get("/suites/{name}/run.junit", tags=["runs"], dependencies=guarded)
    def run_suite_junit(name: str, na: NexAssure = Depends(get_nexassure)) -> PlainTextResponse:
        """Run a suite and return JUnit XML, for CI systems that poll."""
        from ..reporting.exporters import to_junit

        run = na.run_suite(name, triggered_by="api", notify=False)
        return PlainTextResponse(to_junit(run), media_type="application/xml")

    # ------------------------------------------------------------- runs ---

    @app.get("/runs", tags=["runs"], dependencies=guarded)
    def list_runs(
        suite: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        na: NexAssure = Depends(get_nexassure),
    ) -> dict[str, Any]:
        """List recorded runs, newest first."""
        return {"runs": na.history(suite, limit)}

    @app.get("/runs/{run_id}", tags=["runs"], dependencies=guarded)
    def get_run(run_id: str, na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """Fetch one run with all its results."""
        run = na.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"No run with id {run_id!r}")
        return run

    @app.get("/runs/{run_id}/report", response_class=HTMLResponse, tags=["runs"])
    def run_report(run_id: str, na: NexAssure = Depends(get_nexassure)) -> HTMLResponse:
        """Render a recorded run as a shareable HTML page."""
        from ..core.models import CheckResult, RunResult
        from ..reporting.exporters import to_html

        stored = na.get_run(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"No run with id {run_id!r}")

        run = RunResult(
            run_id=stored["run_id"],
            suite_name=stored["suite_name"],
            connection_name=stored["connection_name"],
            started_at=stored["started_at"],
            finished_at=stored.get("finished_at"),
            duration_ms=stored.get("duration_ms") or 0.0,
            environment=stored.get("environment"),
            results=[
                CheckResult(
                    check_id=r["check_id"],
                    check_name=r["check_name"],
                    check_type=r["check_type"],
                    status=r["status"],
                    severity=r["severity"],
                    description=r.get("description"),
                    dataset=r.get("dataset_fqn"),
                    column=r.get("column_name"),
                    observed=r.get("observed"),
                    expected=r.get("expected"),
                    message=r.get("message") or "",
                    rows_scanned=r.get("rows_scanned"),
                    rows_failed=r.get("rows_failed"),
                    failed_ratio=r.get("failed_ratio"),
                    sample_rows=r.get("sample_rows") or [],
                    query=r.get("query"),
                    duration_ms=r.get("duration_ms") or 0.0,
                    started_at=r["started_at"],
                    error=r.get("error"),
                )
                for r in stored.get("results", [])
            ],
        ).recompute()
        return HTMLResponse(to_html(run))

    @app.get("/summary", tags=["runs"], dependencies=guarded)
    def summary(
        hours: int = Query(24, ge=1, le=8760), na: NexAssure = Depends(get_nexassure)
    ) -> dict[str, Any]:
        """Headline quality numbers over a recent window."""
        return na.summary(hours)

    @app.get("/failures", tags=["runs"], dependencies=guarded)
    def failures(
        hours: int = Query(24, ge=1, le=8760),
        limit: int = Query(100, ge=1, le=1000),
        na: NexAssure = Depends(get_nexassure),
    ) -> dict[str, Any]:
        """List checks that failed recently."""
        store = na.metastore
        return {"failures": store.failing_checks(hours, limit) if store else []}

    # ---------------------------------------------------------- catalog ---

    @app.get("/catalog/datasets", tags=["catalog"], dependencies=guarded)
    def catalog_datasets(
        connection: str | None = Query(None),
        schema: str | None = Query(None),
        limit: int = Query(200, ge=1, le=5000),
        na: NexAssure = Depends(get_nexassure),
    ) -> dict[str, Any]:
        """List datasets recorded in the metastore."""
        store = na.metastore
        return {"datasets": store.list_datasets(connection, schema, limit) if store else []}

    @app.get("/catalog/profiles/{fqn}", tags=["catalog"], dependencies=guarded)
    def catalog_profile(fqn: str, na: NexAssure = Depends(get_nexassure)) -> dict[str, Any]:
        """Fetch the most recent profile snapshot for a dataset."""
        store = na.metastore
        profile = store.latest_profile(fqn) if store else None
        if profile is None:
            raise HTTPException(status_code=404, detail=f"No profile recorded for {fqn!r}")
        return profile

    return app
