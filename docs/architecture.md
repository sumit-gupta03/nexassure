# Architecture

How NexAssure is put together, and why.

## The shape

```
                    CLI        REST API        MCP server
                     |            |                |
                     +------------+----------------+
                                  |
                            NexAssure facade                  (api.py)
                                  |
        +-----------+-------------+--------------+-----------+
        |           |             |              |           |
    Connectors    Checks     SuiteRunner     Profiler    Metastore
        |           |             |              |           |
      Dialect   Expectations   Waves +      Batched      SQLAlchemy
                              ThreadPool    aggregates      Core
        |                                       |           |
        +---------------- warehouse ------------+       history DB
```

Three rules explain most of the design:

1. **One facade.** The CLI, the REST API and the MCP server are thin shells
   over `NexAssure` in `api.py`. They cannot drift apart in behaviour, because
   they share every code path that matters.
2. **Dialect is the only place engines differ.** A check emits ANSI SQL through
   `ctx.dialect`; each connector overrides only the fragments its engine spells
   differently. That is what lets one `not_null` definition run on nine engines.
3. **Everything is a validated model.** Config, suites, results and profiles are
   Pydantic models, so the YAML loader, the API and the MCP server share one
   schema and one set of error messages.

## Modules

| Module | Responsibility |
|---|---|
| `core/models.py` | The domain model. Every input and output type |
| `core/enums.py` | Severity, statuses, operators, result shapes |
| `core/engine.py` | `SuiteRunner`: dependency waves, parallelism, fail-fast |
| `connectors/base.py` | `BaseConnector` and `Dialect` |
| `connectors/registry.py` | Lazy resolution, entry-point plugins |
| `checks/base.py` | `Check`, `RowPredicateCheck`, the check registry |
| `checks/builtin.py` | Built-in check types |
| `checks/custom_sql.py` | Description + query + expected output |
| `checks/expectations.py` | Reduce a result set, then compare it |
| `profiling/profiler.py` | Batched aggregate profiling |
| `profiling/inference.py` | Profile to starter suite |
| `metastore/` | Catalog, check registry, run history |
| `suites/loader.py` | YAML parsing, offline validation |
| `scheduler/` | Cron scheduling |
| `reporting/` | Terminal, JSON, JUnit, Markdown, HTML |
| `utils/sqlsafe.py` | The read-only SQL guard |
| `api.py` | The facade |

## Key decisions

### Checks generate SQL; they do not pull data

A check runs an aggregate on the warehouse and gets back a number. It never
pulls a table into Python to inspect it. That is what makes NexAssure usable on a
billion-row fact table, and it is why `Dialect` has to exist.

The only rows that ever cross the wire are the handful of sample failing rows
captured for triage — bounded by `sample_limit`.

### `RowPredicateCheck` collapses most check types into one line

Most checks answer "which rows are bad?". That whole pattern is implemented
once: a subclass supplies a SQL predicate that is true for failing rows, and
the base class issues

```sql
SELECT COUNT(*), SUM(CASE WHEN <predicate> THEN 1 ELSE 0 END) FROM <table>
```

then a second query only when there is something to sample. **Two round trips
per check, whatever the check.** `CASE WHEN` is used rather than `FILTER` or
`COUNT_IF` because it works on every supported engine.

Checks needing a `GROUP BY`, a second table, or catalog introspection implement
`evaluate()` directly.

### Profiling batches aggregates

A naive profiler issues one query per metric per column — thousands of round
trips on a 200-column table. NexAssure puts every aggregate for a group of columns
into a single `SELECT`:

```sql
SELECT COUNT(*),
       COUNT(col1), COUNT(DISTINCT col1), MIN(col1), MAX(col1), AVG(col1), ...,
       COUNT(col2), COUNT(DISTINCT col2), ...
FROM table
```

Cost is one query for table counts, `ceil(columns / 25)` for column aggregates,
and one per column only for the optional extras. Columns are aliased by
position (`c0_nonnull`, `c1_min`) because column names collide with keywords and
overflow identifier limits once suffixed.

If a batched query is rejected, it falls back to per-column profiling rather
than failing.

### Metastore writes are best-effort

A metastore outage degrades NexAssure to "still runs your tests, just does not
record history". It never turns a green pipeline red. Every write catches, logs
and continues — unless `strict: true`.

Upserts are hand-rolled `UPDATE`-then-`INSERT` rather than `ON CONFLICT` or
`MERGE`, so one code path works on SQLite, Postgres, MySQL and SQL Server.
Columns that must survive updates (`first_seen_at`, `created_at`) are passed as
an `insert_only` payload.

### Parallelism is bounded three ways

`max_parallel` caps in-flight checks, the connector pool caps real database
sessions, and `depends_on` splits execution into sequential waves. Within a
wave, order is arbitrary and fully parallel.

Single-connection engines (DuckDB, file-backed SQLite) set
`serialized_access = True` and take a lock around database access. Without it,
SQLAlchemy opens a transaction per checkout on a shared connection and
concurrent checks collide with *"cannot start a transaction within a
transaction"*. Real warehouses keep full parallelism.

### Results, not exceptions

`Check.run()` never raises. An exception becomes an `ERRORED` result, so one
broken check cannot abort a suite and hide the other 199 outcomes. The same
principle drives the MCP server returning `{"ok": false, "error": ...}` instead
of a protocol error.

### Exit codes distinguish two failures

`1` means the data is bad. `2` means NexAssure could not run. Collapsing those
into one code makes it impossible to route alerts correctly — a broken
credential and a genuine data defect need different people.

### Expectations are forgiving about types

`Decimal("3")`, `3` and `3.0` compare equal; a `date` compares equal to the ISO
string a user typed in YAML. Databases return these inconsistently across
drivers, and being strict just produces failures that teach people to distrust
the tool.

### The SQL guard parses structure, not text

`utils/sqlsafe.py` strips comments, string literals and quoted identifiers
before matching keywords. Without that, `WHERE action = 'delete'` would be
rejected and a column named `"drop"` would be unusable — and a guard that
blocks legitimate reads is worse than no guard, because people turn it off.

It is a keyword screen, not a SQL parser, and it is explicitly not the security
boundary. See [SECURITY.md](https://github.com/sumit-gupta03/nexassure/blob/main/SECURITY.md).

## Extension points

Both use entry points, so third parties ship engines and check types from their
own packages with no fork:

```toml
[project.entry-points."nexassure.connectors"]
clickhouse = "nexassure_clickhouse:ClickHouseConnector"

[project.entry-points."nexassure.checks"]
my_checks = "my_package.checks:register"
```

Connector classes resolve lazily from a `module:ClassName` string, so importing
NexAssure never imports a driver you have not installed. That is why
`import nexassure` works with zero database drivers present.

## Data flow: one run

```
nexassure run orders_quality
  │
  ├── load_config()                  nexassure.yml, ${env:...} resolved
  ├── load_suites()                  YAML → Suite models
  ├── validate_suite()               offline; unknown types, cycles, bad params
  │
  ├── connect()
  │     ├── create_engine()          driver imported here, not before
  │     ├── metastore.bootstrap()    tables created if missing
  │     └── bootstrap_on_connect()   connection + catalog recorded
  │
  ├── plan_waves()                   depends_on → execution waves
  │
  └── for each wave, in parallel:
        build_check(spec).run(ctx)
          ├── validate()             shape errors, before any SQL
          ├── evaluate()             → Outcome (passed, observed, rows, samples)
          ├── threshold arithmetic
          └── severity → status
                                     ↓
        recompute() → RunResult → metastore.record_run() → report → exit code
```

## Testing

The whole suite runs against DuckDB in a temp directory: no server, no
credentials, no network. That is a deliberate constraint — a test suite needing
a live Snowflake is a test suite nobody runs.

Generated SQL is tested by **executing** it against fixture data with one
planted defect per check family, not by asserting on SQL strings. SQL that
looks right and does not run is exactly the failure mode this project exists to
prevent.

Connector-specific dialect behaviour on engines CI cannot reach (Snowflake,
Oracle, Synapse) is verified by hand; connector PRs should say which version
was tested.
