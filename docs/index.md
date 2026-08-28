# NexAssure

**Open-source data testing and profiling for the modern warehouse — with a built-in MCP server.**

NexAssure connects to your warehouse, catalogs it, profiles it, and runs the
data tests you declare in YAML — in parallel, on a schedule, or from CI. It
records every run, so you see trends instead of one-off pass/fail noise.

It also ships an **MCP server**, so an AI agent can explore your data, propose
tests grounded in a real profile, and run them — without you handing it write
access to anything.

```bash
pip install "nexassure[postgres]"
nexassure init
nexassure test-connection --all
nexassure suggest prod --schema public -o suites/generated.yml
nexassure run
```

---

## Start here

<div class="grid cards" markdown>

-   :material-rocket-launch: **[Getting started](getting-started.md)**

    From nothing to a running test suite in about ten minutes.

-   :material-format-list-checks: **[Check reference](checks.md)**

    All 22 test types, their parameters, and when to reach for each.

-   :material-database: **[Connectors](connectors.md)**

    Per-warehouse setup, dialect quirks, and the minimum grants each needs.

-   :material-history: **[The metastore](metastore.md)**

    What gets recorded on every run, and how to query it for trends.

-   :material-robot: **[MCP server](mcp.md)**

    Letting an agent explore and test your data, read-only by default.

-   :material-sitemap: **[Architecture](architecture.md)**

    How the pieces fit together, and why they are built that way.

</div>

---

## What it does

| | |
|---|---|
| **Nine warehouses** | Snowflake, PostgreSQL, SQL Server, Redshift, Synapse, Oracle, MySQL, DuckDB, SQLite — one test definition runs on all of them |
| **Zero setup** | Metadata tables are created automatically on first connect. No migration step, no separate service |
| **Custom rules** | Description + SQL + expected output is a first-class test type, not an escape hatch |
| **Parallel** | Every test in a suite runs concurrently against one pool. 200 tests in seconds, not minutes |
| **Agent-native** | A real MCP server with 19 tools, read-only by default |
| **CI-native** | JUnit XML, meaningful exit codes, Markdown for PR comments |
| **Apache 2.0** | Permissive. Embed it, fork it, ship it in your product |

## A test suite looks like this

```yaml
name: orders_quality
connection: prod
schedule: "0 6 * * *"

defaults:
  schema: PUBLIC
  severity: error

checks:
  - name: order_id_is_the_key
    type: primary_key
    description: >
      order_id joins to every downstream mart. A duplicate double-counts
      revenue; a NULL drops the row from every inner join.
    dataset: ORDERS
    column: ORDER_ID

  - name: orders_reference_real_customers
    type: referential_integrity
    description: An order with no matching customer breaks revenue attribution.
    dataset: ORDERS
    column: CUSTOMER_ID
    params: {to: CUSTOMERS, field: CUSTOMER_ID}

  - name: revenue_reconciles_with_ledger
    type: custom_sql
    description: Daily revenue must match the finance ledger to within a cent.
    query: |
      SELECT ABS(SUM(o.total) - SUM(l.amount))
      FROM orders o JOIN ledger l ON o.order_date = l.entry_date
      WHERE o.order_date = CURRENT_DATE - 1
    expect:
      operator: lte
      value: 0.01
```

The `description` is not decoration. It is what appears in the report, in
Slack, and in the MCP response an agent reads — the difference between an alert
saying `unique failed` and one that explains the consequence.

## Exit codes

Part of the contract, because this runs in CI:

| Code | Meaning |
|---|---|
| `0` | Everything passed. `warn`-severity failures do not fail the build |
| `1` | At least one test failed or errored |
| `2` | NexAssure could not run: bad config, unreachable database, invalid suite |

That split lets you page differently for "the data is bad" and "the tool is
broken".

## Try it without a warehouse

The quickstart runs against a local DuckDB file — no credentials, no server:

```bash
git clone https://github.com/sumit-gupta03/nexassure.git
cd nexassure/examples/quickstart
pip install "nexassure[duckdb]"
python seed.py
nexassure run
```

The demo data carries one planted defect per test family, so every failure you
see is a real one the suite caught.

## Authors

Created and maintained by **Sumit Kumar Gupta** ([@sumit-gupta03](https://github.com/sumit-gupta03)) and **Nitish Pradhan**.

## Licence

[Apache License 2.0](https://github.com/sumit-gupta03/nexassure/blob/main/LICENSE).
