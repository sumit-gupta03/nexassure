# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Create the demo warehouse used by the quickstart.

Builds a small DuckDB database with deliberately imperfect data, so the example
suite has real defects to find. Run it with:

    python examples/quickstart/seed.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

TARGET = Path(__file__).parent / "warehouse.duckdb"

SCHEMA = """
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    customer_id  VARCHAR,
    email        VARCHAR,
    region       VARCHAR,
    status       VARCHAR,
    signup_date  DATE
);

CREATE TABLE orders (
    order_id     VARCHAR,
    customer_id  VARCHAR,
    status       VARCHAR,
    total        DECIMAL(12, 2),
    created_at   TIMESTAMP
);
"""

REGIONS = ("emea", "namer", "apac", "latam")
CUSTOMER_STATES = ("active", "trial", "churned")
ORDER_STATES = ("pending", "shipped", "delivered", "cancelled")


def build_rows():
    """Generate the demo data, planting one defect per check family."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    customers, orders = [], []

    for i in range(1, 501):
        customers.append(
            (
                f"c{i:04d}",
                f"user{i}@example.com",
                REGIONS[i % len(REGIONS)],
                CUSTOMER_STATES[i % len(CUSTOMER_STATES)],
                (now - timedelta(days=400 - i % 400)).date(),
            )
        )

    # -- planted defects -----------------------------------------------------
    customers.append(("c0007", "duplicate@example.com", "emea", "active", now.date()))
    customers.append(("c0501", None, "emea", "active", now.date()))
    customers.append(("c0502", "   ", "namer", "active", now.date()))
    customers.append(("c0503", "mars@example.com", "mars", "active", now.date()))

    for i in range(1, 1201):
        orders.append(
            (
                f"o{i:05d}",
                f"c{(i % 500) + 1:04d}",
                ORDER_STATES[i % len(ORDER_STATES)],
                round(15 + (i * 7.31) % 900, 2),
                now - timedelta(hours=(i % 240)),
            )
        )

    orders.append(("o99991", "c9999", "shipped", 42.00, now))       # orphan
    orders.append(("o99992", "c0001", "delivered", -19.99, now))    # negative total
    orders.append(("o99993", "c0002", "refunded", 25.00, now))      # unknown status

    return customers, orders


def main() -> None:
    customers, orders = build_rows()
    connection = duckdb.connect(str(TARGET))
    try:
        connection.execute(SCHEMA)
        connection.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)
        connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
    finally:
        connection.close()

    print(f"Seeded {TARGET}")
    print(f"  customers: {len(customers):,} rows")
    print(f"  orders:    {len(orders):,} rows")
    print()
    print("Now run:")
    print("  cd examples/quickstart")
    print("  nexassure test-connection --all")
    print("  nexassure profile demo main.orders")
    print("  nexassure run")


if __name__ == "__main__":
    main()
