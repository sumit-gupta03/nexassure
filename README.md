<div align="center">

# NexAssure

**Open-source data testing and profiling for the modern warehouse — with a built-in MCP server.**

[![PyPI](https://img.shields.io/pypi/v/nexassure.svg)](https://pypi.org/project/nexassure/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org)
[![CI](https://github.com/sumit-gupta03/nexassure/actions/workflows/ci.yml/badge.svg)](https://github.com/sumit-gupta03/nexassure/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-sumit--gupta03.github.io%2Fnexassure-blue)](https://sumit-gupta03.github.io/nexassure/)

</div>

---

NexAssure connects to your warehouse, catalogs it, profiles it, and runs the data
tests you declare in YAML — in parallel, on a schedule, or from CI. It records
every run, so you see trends instead of one-off pass/fail noise.

It also ships an **MCP server**, so an AI agent can explore your data, propose
checks grounded in a real profile, and run them — without you handing it write
access to anything.

```bash
pip install "nexassure[postgres]"
nexassure init
nexassure test-connection --all
nexassure suggest prod --schema public -o suites/generated.yml
nexassure run
```

## Why another data testing tool?

| | NexAssure |
|---|---|
| **Warehouses** | Snowflake, PostgreSQL, SQL Server, Redshift, Synapse, Oracle, MySQL, DuckDB, SQLite — one check definition, every dialect |
| **Zero setup** | Metadata tables are created automatically on first connect. No migration step, no separate service |
| **Custom rules** | Description + SQL + expected output is a first-class check type, not an escape hatch |
| **Parallel** | Every check in a suite runs concurrently against one pool. 200 checks in seconds, not minutes |
| **Agent-native** | A real MCP server with 18 tools, read-only by default |
| **CI-native** | JUnit XML, meaningful exit codes, Markdown for PR comments |
| **Apache 2.0** | Permissive. Embed it, fork it, ship it in your product |

## Install

```bash
pip install nexassure                    # core
pip install "nexassure[snowflake]"       # + Snowflake driver
pip install "nexassure[postgres,mcp]"    # + Postgres and the MCP server
pip install "nexassure[all]"             # everything
```

| Extra | Installs | For |
|---|---|---|
| `postgres` | `psycopg` | PostgreSQL |
| `snowflake` | `snowflake-sqlalchemy` | Snowflake |
| `mssql` / `synapse` | `pyodbc` | SQL Server, Azure SQL, Synapse |
| `redshift` | `redshift-connector` | Amazon Redshift |
| `oracle` | `oracledb` | Oracle Database |
| `mysql` | `pymysql` | MySQL, MariaDB |
| `duckdb` | `duckdb-engine` | DuckDB, local files, Parquet |
| `mcp` | `mcp` | MCP server |
| `server` | `fastapi`, `uvicorn` | REST API |
| `notify` | `httpx` | Slack and webhook alerts |

`nexassure connectors` shows what is installed and what to install next.

## Quick start

### 1. Create a project

```bash
nexassure init
```

This writes `nexassure.yml` and `suites/example.yml`.

```yaml
# nexassure.yml
version: 1
project: analytics-warehouse

connections:
  - name: prod
    type: snowflake
    account: ${env:SNOWFLAKE_ACCOUNT}
    username: ${env:SNOWFLAKE_USER}
    password: ${env:SNOWFLAKE_PASSWORD}
    warehouse: ANALYTICS_WH
    database: PROD
    schema: PUBLIC

suites:
  - suites/**/*.yml
```

Secrets are `${env:VAR}` references resolved at load time, so the file is safe
to commit.

### 2. Connect — metadata tables appear on their own

```bash
nexassure test-connection prod
nexassure discover prod --schema PUBLIC
```

The first time NexAssure opens a connection it creates its metastore tables and
records what it finds. Nothing to migrate, nothing to bootstrap:

| Table | Holds |
|---|---|
| `nexassure_connections` | Every data source seen. Never credentials |
| `nexassure_datasets` | Tables and views discovered, with descriptions |
| `nexassure_columns` | Columns, types, nullability, first/last seen |
| `nexassure_checks` | Registered check definitions |
| `nexassure_runs` | One row per suite execution |
| `nexassure_check_results` | One row per check per run — the trend table |
| `nexassure_profiles` / `nexassure_column_profiles` | Profiling snapshots over time |
| `nexassure_schedules` | Cron schedules and their last outcome |

By default this lives in a SQLite file under `~/.nexassure`. Point
`metastore.url` at Postgres to share history across a team or CI fleet.

### 3. Profile

```bash
nexassure profile prod PUBLIC.ORDERS
```

```
──────────────────── PROD.PUBLIC.ORDERS ────────────────────
1,284,391 rows  14 columns  0 duplicate rows  1.31s

Column         Type         Nulls            Distinct            Min         Max
order_id       VARCHAR      0 (0.0%)         1,284,391 (unique)  0000a1      fffe92
customer_id    VARCHAR      0 (0.0%)         48,201              000012      ffff01
status         VARCHAR      0 (0.0%)         4                   cancelled   shipped
total          NUMERIC      1,204 (0.1%)     92,847              -49.99      18,400.00
created_at     TIMESTAMP    0 (0.0%)         1,102,884           2019-03-01  2026-08-27
```

Profiling is batched: every aggregate for a group of columns goes in one
`SELECT`, so a 200-column table costs a handful of scans, not thousands.

### 4. Generate a starter suite

```bash
nexassure suggest prod --table PUBLIC.ORDERS -o suites/orders.yml
```

NexAssure proposes only what the data justifies — `not_null` on columns that are
fully populated, `unique` on columns with no repeats, `accepted_values` on
low-cardinality enums, padded `range` bounds on numerics. Everything is
severity `warn` and tagged `auto-suggested`, so a generated suite can never
break your pipeline before a human has reviewed it.

### 5. Write checks

```yaml
name: orders_quality
connection: prod
description: Contract for the orders fact table.
schedule: "0 6 * * *"

defaults:
  schema: PUBLIC
  severity: error

checks:
  - name: order_id_is_the_key
    type: primary_key
    description: order_id joins to every downstream mart. A duplicate double-counts revenue.
    dataset: ORDERS
    column: ORDER_ID

  - name: status_is_a_known_state
    type: accepted_values
    description: The BI layer only renders these four states; anything else shows as blank.
    dataset: ORDERS
    column: STATUS
    params:
      values: [pending, shipped, delivered, cancelled]

  - name: orders_have_real_customers
    type: referential_integrity
    description: An order with no matching customer breaks revenue attribution.
    dataset: ORDERS
    column: CUSTOMER_ID
    params:
      to: CUSTOMERS
      field: CUSTOMER_ID

  - name: loaded_within_the_hour
    type: freshness
    description: The hourly pipeline is late if the newest row is over 90 minutes old.
    dataset: ORDERS
    column: CREATED_AT
    params:
      max_age_minutes: 90

  - name: totals_are_never_negative
    type: range
    description: A refund belongs in the refunds table, not as a negative order.
    dataset: ORDERS
    column: TOTAL
    threshold: 0.001        # tolerate up to 0.1% while the backfill lands
    params:
      min: 0
```

### 6. Run

```bash
nexassure run orders_quality
```

```
PASS  order_id_is_the_key [PROD.PUBLIC.ORDERS.ORDER_ID] 412ms
PASS  status_is_a_known_state [PROD.PUBLIC.ORDERS.STATUS] 388ms
FAIL  orders_have_real_customers [PROD.PUBLIC.ORDERS.CUSTOMER_ID] 921ms
PASS  loaded_within_the_hour [PROD.PUBLIC.ORDERS.CREATED_AT] 104ms
PASS  totals_are_never_negative [PROD.PUBLIC.ORDERS.TOTAL] 350ms

╭─ orders_have_real_customers  (referential_integrity) ─────────────────╮
│ Why it matters: An order with no matching customer breaks revenue     │
│                 attribution.                                          │
│ Expected: 0                                                           │
│ Observed: 47                                                          │
│ Rows: 47 failing of 1,284,391 scanned (0.00%)                         │
│                                                                       │
│ Sample failing rows:                                                  │
│ orphan_value                                                          │
│ ------------                                                          │
│ c_99183                                                               │
│ c_99184                                                               │
╰───────────────────────────────────────────────────────────────────────╯

FAILED  4 passed  1 failed  (5 checks in 0.94s, run a3f81c22)
```

## Custom rules: description + query + expected output

The check type most teams reach for. Say what the rule means, write the SQL,
declare the answer:

```yaml
- name: revenue_reconciles_with_ledger
  type: custom_sql
  description: Daily revenue must match the finance ledger to within a cent.
  query: |
    SELECT ABS(SUM(o.total) - SUM(l.amount))
    FROM orders o
    JOIN ledger l ON o.order_date = l.entry_date
    WHERE o.order_date = CURRENT_DATE - 1
  expect:
    operator: lte
    value: 0.01
```

`expect` has two parts. **`shape`** reduces the result set, **`operator`**
compares it:

| `shape` | Reduces to |
|---|---|
| `scalar` *(default)* | First column of the first row |
| `row` | The first row as a list |
| `column` | The first column as a list |
| `table` | All rows |
| `row_count` | Just the number of rows |

| `operator` | Meaning |
|---|---|
| `eq` `ne` | Equal / not equal (tolerant of Decimal vs int vs float) |
| `gt` `gte` `lt` `lte` | Ordering |
| `between` | `value: [low, high]` |
| `in` `not_in` | Membership |
| `matches` `not_matches` | Regex |
| `contains` | Substring or list membership |
| `approx` | Numeric with `tolerance` / `relative_tolerance` |
| `set_equals` | Same values, order-insensitive |
| `rows_equal` | Full result set match |
| `empty` `not_empty` `is_null` `is_not_null` | Presence |

More shapes:

```yaml
# The row-per-violation style. Every row returned is a defect.
- name: no_future_dated_orders
  type: sql_returns_no_rows
  description: An order dated in the future means a timezone bug in the loader.
  query: SELECT order_id, created_at FROM orders WHERE created_at > CURRENT_TIMESTAMP

# Exact expected result set.
- name: region_split_is_stable
  type: custom_sql
  description: All five regions must report, or a partition failed to load.
  query: SELECT region FROM orders GROUP BY region ORDER BY region
  expect:
    shape: column
    operator: set_equals
    value: [apac, emea, latam, namer, other]

# Reconcile two systems.
- name: staging_matches_source
  type: compare_queries
  description: Row counts per day must be identical after the migration.
  query:       SELECT day, COUNT(*) FROM staging.orders GROUP BY day
  params:
    other_query: SELECT day, COUNT(*) FROM legacy.orders GROUP BY day
```

## Built-in checks

| Family | Types |
|---|---|
| **Completeness** | `not_null`, `not_blank`, `completeness` |
| **Uniqueness** | `unique`, `primary_key`, `no_duplicate_rows` |
| **Volume** | `row_count`, `not_empty` |
| **Validity** | `accepted_values`, `rejected_values`, `range`, `regex`, `length` |
| **Timeliness** | `freshness` |
| **Consistency** | `referential_integrity`, `schema`, `column_exists` |
| **Statistical** | `aggregate` |
| **Custom** | `custom_sql`, `sql_returns_no_rows`, `sql_returns_rows`, `compare_queries` |

`nexassure checks` lists them all with their parameters.

Every check supports `severity` (`info` / `warn` / `error` / `critical`),
`threshold` (a ratio when ≤ 1, an absolute row count above it), `where`,
`tags`, `owner`, and `depends_on`.

## Running everything together, or on a schedule

Checks in a suite are independent, so they all run **concurrently** against one
connection pool:

```bash
nexassure run                      # every suite
nexassure run orders_quality       # one suite
nexassure run --tag critical       # only critical checks
nexassure run --dataset PROD.PUBLIC.ORDERS
nexassure run --parallel 16
```

`depends_on` splits execution into sequential waves when a cheap check should
gate an expensive one:

```yaml
- name: orders_not_empty
  type: not_empty
  dataset: ORDERS

- name: orders_deep_scan
  type: no_duplicate_rows
  dataset: ORDERS
  depends_on: [orders_not_empty]   # skipped, not failed, if the table is empty
```

For scheduling, either give a suite a `schedule:` and run the built-in
scheduler:

```bash
nexassure schedule list
nexassure schedule run            # foreground process, fires suites on their cron
```

…or call `nexassure run` from Airflow, Dagster, GitHub Actions or a Kubernetes
CronJob. The scheduler never overlaps a suite with itself and never replays
missed windows after downtime — one late pipeline should not produce an alert
storm.

## CI

```yaml
# .github/workflows/data-quality.yml
- run: pip install "nexassure[snowflake]"
- run: nexassure run --output results.xml --format junit
  env:
    SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
    SNOWFLAKE_USER: ${{ secrets.SNOWFLAKE_USER }}
    SNOWFLAKE_PASSWORD: ${{ secrets.SNOWFLAKE_PASSWORD }}
- uses: mikepenz/action-junit-report@v4
  if: always()
  with:
    report_paths: results.xml
```

Exit codes are part of the contract:

| Code | Meaning |
|---|---|
| `0` | Everything passed. `warn`-severity failures do not fail the build |
| `1` | At least one check failed or errored |
| `2` | NexAssure could not run: bad config, unreachable database, invalid suite |

That split lets you page differently for "the data is bad" and "the tool is
broken". Reports render as `--format json | junit | markdown | html`.

## MCP server

```bash
nexassure mcp
```

```json
{
  "mcpServers": {
    "nexassure": {
      "command": "nexassure",
      "args": ["mcp", "--config", "/path/to/nexassure.yml"]
    }
  }
}
```

| Tool | Does |
|---|---|
| `nexassure_info` | Version, project, what is installed |
| `list_connections` · `test_connection` | Discover and verify data sources |
| `list_schemas` · `list_tables` · `describe_table` | Explore the catalog |
| `discover_catalog` | Record the catalog in the metastore |
| `profile_table` | Full profile in one batched pass |
| `suggest_checks` | Propose a suite grounded in a real profile |
| `list_check_types` | The check vocabulary, with parameters |
| `run_check` | Run one ad-hoc check, no file needed |
| `run_suite` · `list_suites` · `validate_suites` | Execute and lint suites |
| `run_query` | Read-only SQL |
| `quality_summary` · `recent_failures` · `run_history` · `get_run` | Trends and triage |
| `save_suite` | Write a suite file — only with `--allow-writes` |

Safety properties that make this usable against a real warehouse:

- **Read-only by default.** Every SQL path goes through the same guard: only
  `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN`, single statement, write
  keywords rejected outside string literals. An agent cannot drop a table.
- **Bounded output.** Rows, columns and long values are capped so one call
  cannot flood a context window.
- **Errors are values.** Failures return `{"ok": false, "error": ...}` so the
  agent can read the reason and adapt.
- **File writes are opt-in.** `save_suite` only exists under `--allow-writes`.

This is defence in depth, not a substitute for permissions. Point NexAssure at a
role that only holds `SELECT`.

## REST API

```bash
pip install "nexassure[server]"
nexassure serve --port 8080     # OpenAPI docs at /docs
```

`/health` · `/ready` · `/connections` · `/suites` · `/suites/{name}/run` ·
`/runs/{id}/report` (shareable HTML) · `/summary` · `/failures` ·
`/catalog/datasets`. Set `NEXASSURE_API_TOKEN` to require a bearer token.

## Python API

```python
from nexassure import NexAssure
from nexassure.profiling import ProfileOptions

with NexAssure() as na:
    profile = na.profile("prod", "PUBLIC.ORDERS", ProfileOptions(include_percentiles=True))
    for column in profile.columns:
        if column.null_ratio > 0.1:
            print(f"{column.column} is {column.null_ratio:.1%} null")

    run = na.run_suite("orders_quality")
    for failure in run.failures():
        print(failure.check_name, failure.message, failure.sample_rows)
```

## Extending

Register a warehouse or a check type through entry points — no fork required:

```toml
[project.entry-points."nexassure.connectors"]
clickhouse = "nexassure_clickhouse:ClickHouseConnector"

[project.entry-points."nexassure.checks"]
my_checks = "my_package.checks:register"
```

A new check type is usually a few lines, because `RowPredicateCheck` already
counts and samples failing rows:

```python
from nexassure.checks import CheckContext, RowPredicateCheck, register_check

@register_check
class EmailLooksValidCheck(RowPredicateCheck):
    """Values look like email addresses."""
    type_name = "email_valid"
    requires_column = True
    violation_noun = "malformed emails"

    def failing_predicate(self, ctx: CheckContext) -> str:
        column = self.col(ctx)
        pattern = ctx.dialect.string_literal(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
        return f"({column} IS NOT NULL AND NOT ({ctx.dialect.regexp_match(column, pattern)}))"
```

`Dialect` is the seam that keeps it portable: subclasses override only the
fragments their engine spells differently.

## Documentation

- [Getting started](docs/getting-started.md)
- [Check reference](docs/checks.md)
- [Connectors](docs/connectors.md)
- [The metastore](docs/metastore.md)
- [MCP server](docs/mcp.md)
- [Architecture](docs/architecture.md)

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
The full test suite runs against DuckDB with no external services:

```bash
pip install -e ".[dev]"
pytest
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
