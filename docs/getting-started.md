# Getting started

From nothing to a running data test suite in about ten minutes.

## Install

```bash
pip install "nexassure[postgres]"     # swap in your warehouse extra
```

`nexassure connectors` shows what is installed and what to install next:

```
Type        Driver              Installed  Install with
postgres    psycopg             yes
snowflake   snowflake.sqlalchemy no        pip install 'nexassure[snowflake]'
mssql       pyodbc               no        pip install 'nexassure[mssql]'
```

## 1. Create a project

```bash
mkdir data-quality && cd data-quality
nexassure init
```

That writes `nexassure.yml` and `suites/example.yml`.

## 2. Configure a connection

Edit `nexassure.yml`. Secrets are `${env:VAR}` references resolved at load time,
so the file is safe to commit:

```yaml
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
    role: DATA_QUALITY_READER

suites:
  - suites/**/*.yml
```

`${env:VAR:-default}` supplies a fallback. An unset reference with no default
is a hard error at load time, so a missing secret fails immediately with a
clear message rather than as a confusing connection error later.

> **Use a read-only role.** NexAssure screens every statement, but that is defence
> in depth. The real boundary is database permissions.

Verify it:

```bash
nexassure test-connection --all
```

```
OK    prod (snowflake, 412ms, 8.32.1)
```

## 3. Let it catalog itself

```bash
nexassure discover prod --schema PUBLIC
```

On first connect, NexAssure creates its metastore tables and records what it
finds. There is no migration step. See [the metastore](metastore.md) for what
gets stored.

```bash
nexassure metastore catalog        # what it found
nexassure tables prod              # straight from the warehouse
```

## 4. Profile before you write checks

Do not guess at what your data looks like:

```bash
nexassure profile prod PUBLIC.ORDERS
```

```
──────────────────── PROD.PUBLIC.ORDERS ────────────────────
1,284,391 rows  14 columns  1.31s

Column         Type       Nulls          Distinct            Min         Max
order_id       VARCHAR    0 (0.0%)       1,284,391 (unique)  0000a1      fffe92
customer_id    VARCHAR    0 (0.0%)       48,201              000012      ffff01
status         VARCHAR    0 (0.0%)       4                   cancelled   shipped
total          NUMERIC    1,204 (0.1%)   92,847              -49.99      18,400.00
created_at     TIMESTAMP  0 (0.0%)       1,102,884           2019-03-01  2026-08-27
```

Two things jump out of that output: `total` has negative values, and it has
1,204 NULLs. Both are checks worth writing.

Useful flags:

```bash
nexassure profile prod PUBLIC.ORDERS --percentiles      # median, p25, p75, p95
nexassure profile prod PUBLIC.ORDERS --duplicates       # whole-row duplicates (expensive)
nexassure profile prod PUBLIC.ORDERS --sample 100000    # sample a huge table
nexassure profile prod --schema PUBLIC --limit 20       # profile the whole schema
nexassure profile prod PUBLIC.ORDERS -o profile.json    # machine-readable
```

## 5. Generate a starter suite

```bash
nexassure suggest prod --schema PUBLIC -o suites/generated.yml
```

NexAssure proposes only what the data justifies: `not_null` on columns that are
fully populated, `unique` on columns with no repeats, `accepted_values` on
low-cardinality enums, padded `range` bounds on numerics.

Everything comes out as severity `warn` and tagged `auto-suggested`, so a
generated suite cannot break your pipeline before a human has reviewed it.

**Read it and cut it down.** A generated suite is a starting point, not a
contract. Delete the checks that do not reflect a real expectation, tighten the
ones that do, and promote them to `severity: error`.

## 6. Write the checks that matter

The suggestions cover the mechanical checks. The valuable ones encode knowledge
that is not in the data:

```yaml
name: orders_quality
connection: prod
description: Contract for the orders fact table.

defaults:
  schema: PUBLIC
  severity: error
  owner: data-platform@example.com

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

  - name: loaded_within_the_hour
    type: freshness
    description: The hourly pipeline is late if the newest row is over 90 minutes old.
    dataset: ORDERS
    column: CREATED_AT
    params: {max_age_minutes: 90}

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

Write the `description`. It is what appears in the report, in Slack, and in the
MCP response an agent reads — the difference between an alert that says
`unique failed` and one that explains the consequence.

See the [check reference](checks.md) for every type and parameter.

## 7. Validate offline

```bash
nexassure validate
```

This connects to nothing. It catches unknown check types, missing required
parameters, unknown dependencies and dependency cycles — fast enough for a
pre-commit hook.

## 8. Run

```bash
nexassure run
```

Checks run concurrently against one connection pool, so a 200-check suite takes
seconds rather than minutes.

```bash
nexassure run orders_quality              # one suite
nexassure run --select order_id_is_the_key
nexassure run --tag critical
nexassure run --dataset PROD.PUBLIC.ORDERS
nexassure run --parallel 16
nexassure run --dry-run                   # show the plan, touch nothing
nexassure run --show-sql                  # print the SQL behind each failure
nexassure run --all                       # show passing checks too
```

## 9. Wire it into CI

```yaml
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
| `0` | Everything passed. `warn` failures do not fail the build |
| `1` | At least one check failed or errored |
| `2` | NexAssure could not run: bad config, unreachable database, invalid suite |

That split lets you page differently for "the data is bad" and "the tool is
broken".

Report formats: `--format json | junit | markdown | html`. Markdown is sized for
a pull-request comment; HTML is a self-contained page you can attach as an
artifact.

## 10. Schedule it

Either give the suite a cron expression and run the built-in scheduler:

```yaml
schedule: "0 6 * * *"
```

```bash
nexassure schedule list
nexassure schedule run       # foreground process, fires suites on their cron
```

…or call `nexassure run` from Airflow, Dagster, or a Kubernetes CronJob. If you
already have an orchestrator, use it — the built-in scheduler exists so that a
single container is enough when you do not.

## 11. Watch the trend

Every run is recorded:

```bash
nexassure history
nexassure metastore info
```

```python
from nexassure import NexAssure

with NexAssure() as na:
    print(na.summary(hours=24))
    for failure in na.metastore.failing_checks(since_hours=24):
        print(failure["check_name"], failure["message"])
```

## Notifications

```yaml
notifications:
  notify_on: failure       # always | failure | never
  slack_webhook: ${env:SLACK_WEBHOOK_URL}
  webhook_url: ${env:PAGERDUTY_WEBHOOK}
```

```bash
pip install "nexassure[notify]"
```

Delivery is best-effort and time-boxed: a slow or broken webhook never extends
or fails a run.

## Where to go next

- [Check reference](checks.md) — every type and parameter
- [Connectors](connectors.md) — per-warehouse setup and quirks
- [The metastore](metastore.md) — what gets recorded, and how to query it
- [MCP server](mcp.md) — letting an agent explore and test your data
- [Architecture](architecture.md) — how the pieces fit together
