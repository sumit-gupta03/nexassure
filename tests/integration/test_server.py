# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""REST API endpoints."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastapi", reason="REST API needs: pip install 'nexassure[server]'")


@pytest.fixture
def client(project_dir):
    from fastapi.testclient import TestClient

    from nexassure.server.app import create_app

    with TestClient(create_app(project_dir / "nexassure.yml")) as test_client:
        yield test_client


class TestProbes:
    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_ready_reports_the_project(self, client):
        payload = client.get("/ready").json()
        assert payload["project"] == "test_project"
        assert payload["metastore"] == "ok"

    def test_openapi_schema_is_generated(self, client):
        assert client.get("/openapi.json").status_code == 200


class TestConnections:
    def test_list_redacts_secrets(self, client):
        payload = client.get("/connections").json()
        assert payload["connections"][0]["name"] == "test_warehouse"
        assert payload["connections"][0]["password"] is None

    def test_test_endpoint(self, client):
        assert client.get("/connections/test_warehouse/test").json()["ok"] is True

    def test_tables(self, client):
        payload = client.get("/connections/test_warehouse/tables?schema=main").json()
        assert "main.orders" in payload["tables"]

    def test_describe_table(self, client):
        payload = client.get("/connections/test_warehouse/tables/main.customers").json()
        assert {c["name"] for c in payload["columns"]} >= {"id", "email"}


class TestQuery:
    def test_read_query(self, client):
        response = client.post(
            "/connections/test_warehouse/query",
            json={"sql": "SELECT COUNT(*) AS n FROM main.orders"},
        )
        assert response.status_code == 200
        assert response.json()["rows"][0]["n"] == 7

    def test_write_query_is_rejected(self, client):
        response = client.post(
            "/connections/test_warehouse/query", json={"sql": "DROP TABLE main.orders"}
        )
        assert response.status_code == 400
        assert response.json()["code"] == "unsafe_sql"


class TestSuitesAndRuns:
    def test_list_suites(self, client):
        payload = client.get("/suites").json()
        assert payload["suites"][0]["name"] == "orders_quality"
        assert payload["suites"][0]["check_count"] == 5

    def test_get_suite(self, client):
        assert client.get("/suites/orders_quality").json()["connection"] == "test_warehouse"

    def test_run_suite(self, client):
        payload = client.post("/suites/orders_quality/run", json={}).json()
        assert payload["status"] == "failed"
        assert payload["summary"]["total"] == 5

    def test_run_with_a_selection(self, client):
        payload = client.post(
            "/suites/orders_quality/run", json={"select": ["orders_have_rows"]}
        ).json()
        assert payload["summary"]["total"] == 1
        assert payload["status"] == "passed"

    def test_junit_endpoint(self, client):
        response = client.get("/suites/orders_quality/run.junit")
        assert response.status_code == 200
        assert response.text.startswith("<?xml")

    def test_runs_are_listed_and_fetchable(self, client):
        run_id = client.post("/suites/orders_quality/run", json={}).json()["run_id"]
        assert run_id in {r["run_id"] for r in client.get("/runs").json()["runs"]}
        assert client.get(f"/runs/{run_id}").json()["suite_name"] == "orders_quality"

    def test_html_report_renders(self, client):
        run_id = client.post("/suites/orders_quality/run", json={}).json()["run_id"]
        response = client.get(f"/runs/{run_id}/report")
        assert response.status_code == 200
        assert response.text.lstrip().startswith("<!doctype html>")
        assert "orders_quality" in response.text

    def test_unknown_run_is_404(self, client):
        assert client.get("/runs/deadbeef").status_code == 404

    def test_unknown_suite_is_404(self, client):
        assert client.get("/suites/ghost").status_code == 404


class TestProfilingEndpoints:
    def test_profile(self, client):
        payload = client.post("/connections/test_warehouse/profile?table=main.customers").json()
        assert payload["row_count"] == 7

    def test_suggest(self, client):
        payload = client.post("/connections/test_warehouse/suggest?schema=main").json()
        assert payload["suite"]["checks"]
        assert "checks:" in payload["yaml"]


class TestObservability:
    def test_summary_and_failures(self, client):
        client.post("/suites/orders_quality/run", json={})
        assert client.get("/summary").json()["runs"] >= 1
        assert client.get("/failures").json()["failures"]

    def test_catalog(self, client):
        client.post("/connections/test_warehouse/discover")
        assert client.get("/catalog/datasets").json()["datasets"]


class TestAuth:
    def test_a_token_is_required_when_configured(self, project_dir, monkeypatch):
        from fastapi.testclient import TestClient

        from nexassure.server.app import create_app

        monkeypatch.setenv("NEXASSURE_API_TOKEN", "s3cret")
        with TestClient(create_app(project_dir / "nexassure.yml")) as client:
            assert client.get("/connections").status_code == 401
            assert (
                client.get("/connections", headers={"Authorization": "Bearer wrong"}).status_code
                == 401
            )
            assert (
                client.get("/connections", headers={"Authorization": "Bearer s3cret"}).status_code
                == 200
            )
            # Probes stay open so orchestrators can health-check without a token.
            assert client.get("/health").status_code == 200
