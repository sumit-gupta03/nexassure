# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures.

Everything runs against DuckDB in a temp directory: no server, no credentials,
no network. That keeps the suite fast enough to run on every commit and means a
new contributor can run it immediately after cloning.

The fixture data is deliberately dirty. Each defect below exists so a specific
check has something real to catch:

* ``customers.email`` has a NULL and a blank        -> not_null, not_blank
* ``customers.region`` has one out-of-set value     -> accepted_values
* ``customers`` has a duplicated id                 -> unique, primary_key
* ``orders.customer_id`` has an orphan              -> referential_integrity
* ``orders.total`` has a negative value             -> range, custom_sql
* ``orders.created_at`` has an old max timestamp    -> freshness
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexassure.core.models import ConnectionConfig

SEED_SQL = """
CREATE TABLE customers (
    id           VARCHAR,
    email        VARCHAR,
    region       VARCHAR,
    signup_date  DATE,
    lifetime_value DOUBLE
);

INSERT INTO customers VALUES
    ('c1', 'ada@example.com',     'emea',  DATE '2024-01-15', 1200.50),
    ('c2', 'grace@example.com',   'namer', DATE '2024-02-20',  840.00),
    ('c3', NULL,                  'apac',  DATE '2024-03-10',  310.25),
    ('c4', '   ',                 'emea',  DATE '2024-04-05',  990.00),
    ('c5', 'alan@example.com',    'mars',  DATE '2024-05-01',  150.75),
    ('c5', 'alan.dup@example.com','emea',  DATE '2024-05-02',  150.75),
    ('c6', 'edsger@example.com',  'namer', DATE '2024-06-11', 2400.00);

CREATE TABLE orders (
    order_id     VARCHAR,
    customer_id  VARCHAR,
    status       VARCHAR,
    total        DOUBLE,
    created_at   TIMESTAMP
);

INSERT INTO orders VALUES
    ('o1', 'c1', 'shipped',   120.00, TIMESTAMP '2024-06-01 10:00:00'),
    ('o2', 'c1', 'delivered',  85.50, TIMESTAMP '2024-06-02 11:30:00'),
    ('o3', 'c2', 'pending',   240.00, TIMESTAMP '2024-06-03 09:15:00'),
    ('o4', 'c3', 'cancelled',  60.00, TIMESTAMP '2024-06-04 14:00:00'),
    ('o5', 'c99','shipped',   310.00, TIMESTAMP '2024-06-05 16:45:00'),
    ('o6', 'c4', 'delivered', -45.00, TIMESTAMP '2024-06-06 08:20:00'),
    ('o7', 'c6', 'shipped',   980.25, TIMESTAMP '2024-06-07 19:05:00');

CREATE TABLE clean_table (
    id    INTEGER,
    label VARCHAR
);

INSERT INTO clean_table VALUES (1, 'alpha'), (2, 'beta'), (3, 'gamma');

-- Carries a timestamp column so freshness has something to be empty about.
CREATE TABLE empty_table (id INTEGER, label VARCHAR, created_at TIMESTAMP);
"""


@pytest.fixture(scope="session")
def warehouse_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A DuckDB file seeded with the fixture data."""
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path_factory.mktemp("warehouse") / "test.duckdb"
    connection = duckdb.connect(str(path))
    try:
        for statement in SEED_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)
    finally:
        connection.close()
    return path


@pytest.fixture
def connection_config(warehouse_path: Path) -> ConnectionConfig:
    pytest.importorskip("duckdb_engine")
    return ConnectionConfig(
        name="test_warehouse",
        type="duckdb",
        database=str(warehouse_path),
        schema="main",
    )


@pytest.fixture
def connector(connection_config: ConnectionConfig):
    """A connected DuckDB connector, closed on teardown."""
    from nexassure.connectors.registry import create_connector

    instance = create_connector(connection_config)
    instance.connect()
    yield instance
    instance.close()


@pytest.fixture
def ctx(connector):
    from nexassure.checks.base import CheckContext

    return CheckContext(connector=connector, suite_name="tests")


@pytest.fixture
def metastore(tmp_path: Path):
    """A throwaway SQLite metastore."""
    from nexassure.metastore.repository import Metastore

    store = Metastore(f"sqlite:///{(tmp_path / 'metastore.db').as_posix()}", strict=True)
    store.bootstrap()
    yield store
    store.close()


@pytest.fixture
def project_dir(tmp_path: Path, warehouse_path: Path) -> Path:
    """A complete NexAssure project on disk, pointed at the fixture warehouse."""
    metastore_path = (tmp_path / "metastore.db").as_posix()
    (tmp_path / "nexassure.yml").write_text(
        f"""
version: 1
project: test_project
metastore:
  url: sqlite:///{metastore_path}
  auto_discover: true
connections:
  - name: test_warehouse
    type: duckdb
    database: {warehouse_path.as_posix()}
    schema: main
suites:
  - suites/*.yml
""",
        encoding="utf-8",
    )

    suites = tmp_path / "suites"
    suites.mkdir()
    (suites / "orders.yml").write_text(
        """
name: orders_quality
connection: test_warehouse
description: Fixture suite exercising several check families.
defaults:
  schema: main
  severity: error
checks:
  - name: clean_ids_not_null
    type: not_null
    description: Every row in the clean table has an id.
    dataset: clean_table
    column: id

  - name: clean_ids_unique
    type: unique
    dataset: clean_table
    column: id

  - name: orders_have_rows
    type: row_count
    dataset: orders
    params:
      min: 1

  - name: customer_ids_are_duplicated
    type: unique
    description: Deliberately failing - c5 appears twice.
    dataset: customers
    column: id

  - name: no_negative_totals
    type: custom_sql
    description: Deliberately failing - o6 has a negative total.
    query: SELECT COUNT(*) FROM main.orders WHERE total < 0
    expect:
      operator: eq
      value: 0
""",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def na(project_dir: Path):
    """An NexAssure facade bound to the fixture project."""
    from nexassure.api import NexAssure

    instance = NexAssure(project_dir / "nexassure.yml")
    yield instance
    instance.close()


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only; NexAssure does not target trio."""
    return "asyncio"
