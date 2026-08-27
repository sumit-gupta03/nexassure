# The metastore

The metastore is where NexAssure keeps what it learns: every connection it has
seen, every table and column on it, every check declared, and the full history
of runs, results and profiles.

It is what turns a pass/fail tool into an observability tool. A single run tells
you whether the data is broken right now; the metastore tells you that
`orders.customer_id` has been drifting toward 3% orphans for a fortnight.

## It creates itself

There is no migration command. The first time NexAssure opens a connection, it
creates its tables if they are missing, records the connection, and catalogs
what it finds:

```bash
nexassure test-connection prod     # metastore created, connection registered
nexassure discover prod            # tables and columns catalogued
```

Table creation is idempotent and guarded by a process-level lock, so parallel
check execution cannot race on DDL.

## Where it lives

By default, a SQLite file under `~/.nexassure/metastore.db`. That means a fresh
install works with no setup at all.

For a team, or for CI runners that need shared history, point it at Postgres:

```yaml
metastore:
  url: postgresql+psycopg://nexassure:${env:METASTORE_PASSWORD}@metastore.internal:5432/nexassure
  auto_discover: true
  discovery_limit: 200
  strict: false
  retention_days: 90
```

Or set `NEXASSURE_METASTORE_URL`, which takes precedence over the default but not
over the config file. `NEXASSURE_HOME` relocates the SQLite default.

| Setting | Default | Meaning |
|---|---|---|
| `url` | SQLite in `~/.nexassure` | Any SQLAlchemy URL |
| `enabled` | `true` | Set `false` to run with no history at all |
| `auto_discover` | `true` | Catalog tables and columns whenever a connection opens |
| `discovery_limit` | `200` | Cap on tables catalogued per connect |
| `strict` | `false` | Fail the run when the metastore is unreachable |
| `retention_days` | — | Used by `nexassure metastore purge` |

**It should be a different database from the one under test.** Writing history
into the warehouse you are testing couples the two: a warehouse outage then
takes your quality history with it, and the metastore needs write permission
where NexAssure otherwise needs none.

## Failure is not fatal

Metastore writes are best-effort by design. A metastore outage degrades NexAssure
to "still runs your tests, just does not record history" — it never turns a
green pipeline red. Every write path catches, logs and continues.

Set `strict: true` to invert that, if losing history matters more to you than
completing the run.

## The tables

All prefixed `nexassure_`, so the metastore can share a schema with something else.
Portable types only — no JSONB, no arrays — because the same DDL has to work on
SQLite, Postgres, MySQL and SQL Server. Structured values are stored as JSON
text.

### `nexassure_connections`

Every data source NexAssure has connected to. Host, port, database, schema, server
version, first and last seen, last status.

**Never stores credentials.** No password column exists.

### `nexassure_datasets`

Tables and views discovered by introspection: fully-qualified name, object type,
column count, row count, comment from the source catalog, and a `description`
column for a business description you attach yourself.

`first_seen_at` and `last_seen_at` make schema drift visible: a table whose
`last_seen_at` stops advancing has been dropped or renamed.

### `nexassure_columns`

Columns per dataset: name, ordinal, type, nullability, primary key flag,
default, catalog comment, and your own `description`.

### `nexassure_checks`

Registered check definitions, mirrored from suite files on every run. Checks
removed from a file are removed here too, so the registry always reflects what
is actually declared.

Sync without running anything:

```bash
nexassure metastore sync
```

### `nexassure_runs`

One row per suite execution: status, timings, the six status counters, pass
rate, what triggered it (`manual`, `schedule`, `api`, `mcp`), and environment.

### `nexassure_check_results`

One row per check per run — the table trend queries read from. Carries the
observed and expected values, the message, rows scanned and failed, the SQL
that ran, and the sample failing rows.

> Sample rows are **real data**. On tables holding personal or regulated
> information, set `sample_limit: 0` on the check or in `defaults:`.

Indexed on `(check_id, started_at)`, which is the access path for history.

### `nexassure_profiles` and `nexassure_column_profiles`

Profiling snapshots over time. The column table is indexed on
`(dataset_fqn, column_name, profiled_at)` — the basis for drift detection.

### `nexassure_schedules`

Cron schedules managed by the built-in scheduler, with last and next fire times.

### `nexassure_meta`

Key/value store holding the schema version.

## Reading it

From the CLI:

```bash
nexassure metastore info              # location and headline numbers
nexassure metastore catalog           # datasets discovered
nexassure history --suite orders_quality --limit 20
```

From Python:

```python
from nexassure import NexAssure

with NexAssure() as na:
    store = na.metastore

    store.summary(since_hours=24)
    store.failing_checks(since_hours=24, limit=50)
    store.list_datasets("prod", schema="PUBLIC")
    store.get_dataset_columns("prod", "PROD.PUBLIC.ORDERS")
    store.list_checks(suite_name="orders_quality")
    store.get_run(run_id)

    # Trends
    store.check_history(check_id, limit=30)
    store.profile_history("PROD.PUBLIC.ORDERS", column="TOTAL", limit=30)
    store.latest_profile("PROD.PUBLIC.ORDERS")
```

Or just query it. It is an ordinary database, and the schema is stable:

```sql
-- Which checks fail most often?
SELECT check_name, dataset_fqn,
       COUNT(*) FILTER (WHERE status IN ('failed', 'errored')) AS failures,
       COUNT(*) AS runs
FROM nexassure_check_results
WHERE started_at > NOW() - INTERVAL '30 days'
GROUP BY check_name, dataset_fqn
HAVING COUNT(*) FILTER (WHERE status IN ('failed', 'errored')) > 0
ORDER BY failures DESC;

-- Null-rate drift on one column
SELECT profiled_at, null_ratio, distinct_count, row_count
FROM nexassure_column_profiles
WHERE dataset_fqn = 'PROD.PUBLIC.ORDERS' AND column_name = 'CUSTOMER_ID'
ORDER BY profiled_at;

-- Tables that have never been tested
SELECT d.fqn, d.column_count, d.last_seen_at
FROM nexassure_datasets d
LEFT JOIN nexassure_checks c ON c.dataset_fqn = d.fqn
WHERE c.id IS NULL
ORDER BY d.fqn;
```

That last query is often the most useful thing in here: coverage, not failures.

## Adding descriptions

The catalog carries whatever comments your warehouse exposes, plus descriptions
you attach:

```python
store.set_dataset_description("prod", "PROD.PUBLIC.ORDERS", "The orders fact table.")
store.set_column_description("prod", "PROD.PUBLIC.ORDERS", "TOTAL", "Gross value in USD.")
```

These come back through the REST API and the MCP `discover_catalog` tool, so an
agent exploring your warehouse reads your definitions rather than guessing from
column names.

## Retention

Result and profile history grows linearly with runs multiplied by checks. A
200-check suite running hourly writes about 1.4 million result rows a year —
fine for Postgres, worth watching on SQLite.

```bash
nexassure metastore purge --days 90
```

Deletes runs, results and profiles older than the cutoff, and reports what it
removed. Check definitions, datasets and columns are never purged. Add it to a
weekly cron.

## Schema changes

`nexassure_meta.schema_version` records the DDL version. Within 0.x, a schema change
may require recreating the metastore; the release notes will say so. Constraint
names follow a stable naming convention, so future Alembic migrations can find
them.
