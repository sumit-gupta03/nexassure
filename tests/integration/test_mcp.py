# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""MCP server tools.

The tools are exercised through the FastMCP registry the same way a client
reaches them, so the tests cover the actual wiring - names, schemas and
serialisability - not just the underlying functions.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("mcp", reason="MCP server needs: pip install 'nexassure[mcp]'")


@pytest.fixture
def server(project_dir):
    from nexassure.mcp.server import build_server

    return build_server(project_dir / "nexassure.yml", read_only=True)


async def call(server, name: str, **arguments):
    """Invoke a tool through the MCP registry and return its parsed payload.

    Normalises the three result shapes the SDK has used: a bare list of content
    blocks, a ``(blocks, structured)`` tuple, and a ``CallToolResult`` object.
    """
    result = await server.call_tool(name, arguments)

    content = getattr(result, "content", None)
    structured = getattr(result, "structuredContent", None)
    if content is None:
        if isinstance(result, tuple):
            content, structured = result
        else:
            content = result

    if isinstance(structured, dict):
        # Dict-returning tools are wrapped under "result" by some versions.
        return structured.get("result", structured)
    return json.loads(content[0].text)


class TestToolRegistry:
    @pytest.mark.anyio
    async def test_every_documented_tool_is_registered(self, server):
        registered = {tool.name for tool in await server.list_tools()}
        assert {
            "nexassure_info",
            "list_check_types",
            "list_connections",
            "test_connection",
            "list_schemas",
            "list_tables",
            "describe_table",
            "discover_catalog",
            "profile_table",
            "suggest_checks",
            "list_suites",
            "validate_suites",
            "run_suite",
            "run_check",
            "run_query",
            "quality_summary",
            "recent_failures",
            "run_history",
            "get_run",
        } <= registered

    @pytest.mark.anyio
    async def test_every_tool_has_a_description(self, server):
        for tool in await server.list_tools():
            assert tool.description, f"{tool.name} has no description"

    @pytest.mark.anyio
    async def test_write_tools_are_absent_in_read_only_mode(self, server):
        assert "save_suite" not in {tool.name for tool in await server.list_tools()}

    @pytest.mark.anyio
    async def test_write_tools_appear_when_writes_are_allowed(self, project_dir):
        from nexassure.mcp.server import build_server

        writable = build_server(project_dir / "nexassure.yml", read_only=False)
        assert "save_suite" in {tool.name for tool in await writable.list_tools()}


class TestDiscoveryTools:
    @pytest.mark.anyio
    async def test_info_reports_the_project(self, server):
        payload = await call(server, "nexassure_info")
        assert payload["ok"] is True
        assert payload["project"] == "test_project"
        assert "test_warehouse" in payload["connections"]
        assert payload["read_only"] is True

    @pytest.mark.anyio
    async def test_list_connections_redacts_secrets(self, server):
        payload = await call(server, "list_connections")
        assert [c["name"] for c in payload["connections"]] == ["test_warehouse"]
        assert payload["connections"][0]["password"] is None

    @pytest.mark.anyio
    async def test_test_connection(self, server):
        payload = await call(server, "test_connection", connection="test_warehouse")
        assert payload["ok"] is True

    @pytest.mark.anyio
    async def test_list_tables(self, server):
        payload = await call(server, "list_tables", connection="test_warehouse", schema="main")
        assert "main.customers" in payload["tables"]

    @pytest.mark.anyio
    async def test_describe_table(self, server):
        payload = await call(
            server, "describe_table", connection="test_warehouse", table="main.customers"
        )
        names = {c["name"] for c in payload["dataset"]["columns"]}
        assert names == {"id", "email", "region", "signup_date", "lifetime_value"}

    @pytest.mark.anyio
    async def test_list_check_types(self, server):
        payload = await call(server, "list_check_types")
        types = {c["type"] for c in payload["check_types"]}
        assert {"not_null", "unique", "custom_sql", "freshness"} <= types


class TestProfilingTools:
    @pytest.mark.anyio
    async def test_profile_table(self, server):
        payload = await call(
            server, "profile_table", connection="test_warehouse", table="main.customers"
        )
        assert payload["ok"] is True
        profile = payload["profile"]
        assert profile["row_count"] == 7
        email = next(c for c in profile["columns"] if c["column"] == "email")
        assert email["null_count"] == 1

    @pytest.mark.anyio
    async def test_suggest_checks_returns_usable_yaml(self, server):
        payload = await call(
            server, "suggest_checks", connection="test_warehouse", tables=["main.customers"]
        )
        assert payload["check_count"] > 0
        assert "checks:" in payload["yaml"]

        import yaml as pyyaml

        from nexassure.core.models import Suite
        from nexassure.suites.loader import validate_suite

        assert validate_suite(Suite(**pyyaml.safe_load(payload["yaml"]))) == []


class TestExecutionTools:
    @pytest.mark.anyio
    async def test_run_suite(self, server):
        payload = await call(server, "run_suite", suite="orders_quality")
        assert payload["status"] == "failed"
        assert payload["summary"]["total"] == 5
        assert len(payload["results"]) == 5

    @pytest.mark.anyio
    async def test_run_check_ad_hoc(self, server):
        payload = await call(
            server,
            "run_check",
            connection="test_warehouse",
            check_type="not_null",
            dataset="main.customers",
            column="email",
        )
        assert payload["result"]["status"] == "failed"
        assert payload["result"]["rows_failed"] == 1

    @pytest.mark.anyio
    async def test_run_check_with_a_custom_expectation(self, server):
        payload = await call(
            server,
            "run_check",
            connection="test_warehouse",
            check_type="custom_sql",
            query="SELECT COUNT(*) FROM main.orders",
            expect={"operator": "gte", "value": 5},
        )
        assert payload["result"]["status"] == "passed"

    @pytest.mark.anyio
    async def test_validate_suites(self, server):
        payload = await call(server, "validate_suites")
        assert payload["valid"] is True


class TestQueryTool:
    @pytest.mark.anyio
    async def test_reads_are_allowed(self, server):
        payload = await call(
            server,
            "run_query",
            connection="test_warehouse",
            sql="SELECT COUNT(*) AS n FROM main.orders",
        )
        assert payload["rows"][0]["n"] == 7

    @pytest.mark.anyio
    async def test_writes_are_refused(self, server):
        payload = await call(
            server, "run_query", connection="test_warehouse", sql="DROP TABLE main.orders"
        )
        assert payload["ok"] is False
        assert payload["code"] == "unsafe_sql"

    @pytest.mark.anyio
    async def test_row_limit_is_capped(self, server):
        from nexassure.mcp.server import MAX_QUERY_ROWS

        payload = await call(
            server,
            "run_query",
            connection="test_warehouse",
            sql="SELECT * FROM main.orders",
            max_rows=100_000,
        )
        assert payload["row_count"] <= MAX_QUERY_ROWS


class TestErrorsAreValues:
    """A failing tool must return a readable reason, not a protocol error."""

    @pytest.mark.anyio
    async def test_unknown_connection(self, server):
        payload = await call(server, "test_connection", connection="ghost")
        assert payload["ok"] is False
        assert "Unknown connection" in payload["error"]

    @pytest.mark.anyio
    async def test_unknown_table(self, server):
        payload = await call(
            server, "describe_table", connection="test_warehouse", table="main.ghost"
        )
        assert payload["ok"] is False
        assert payload["error"]

    @pytest.mark.anyio
    async def test_unknown_suite(self, server):
        payload = await call(server, "run_suite", suite="ghost")
        assert payload["ok"] is False
        assert "Unknown suite" in payload["error"]

    @pytest.mark.anyio
    async def test_unknown_check_type(self, server):
        payload = await call(
            server,
            "run_check",
            connection="test_warehouse",
            check_type="not_a_real_check",
            dataset="main.orders",
        )
        assert payload["ok"] is False
        assert "Unknown check type" in payload["error"]


class TestHistoryTools:
    @pytest.mark.anyio
    async def test_summary_and_failures_after_a_run(self, server):
        await call(server, "run_suite", suite="orders_quality")

        summary = await call(server, "quality_summary", hours=24)
        assert summary["summary"]["runs"] >= 1

        failures = await call(server, "recent_failures", hours=24)
        assert failures["failure_count"] >= 2

        history = await call(server, "run_history")
        assert history["runs"]
        fetched = await call(server, "get_run", run_id=history["runs"][0]["run_id"])
        assert fetched["ok"] is True
