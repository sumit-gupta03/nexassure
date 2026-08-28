# Connectors

One check definition runs unchanged on every supported engine. This page covers
how to configure each one, and the engine-specific behaviour worth knowing
before you are surprised by it.

| Engine | `type` | Extra | Driver |
|---|---|---|---|
| Snowflake | `snowflake` | `snowflake` | `snowflake-sqlalchemy` |
| PostgreSQL | `postgres` | `postgres` | `psycopg` (v3) |
| SQL Server / Azure SQL | `mssql` | `mssql` | `pyodbc` |
| Azure Synapse | `synapse` | `synapse` | `pyodbc` |
| Amazon Redshift | `redshift` | `redshift` | `redshift-connector` |
| Oracle | `oracle` | `oracle` | `oracledb` (thin mode) |
| MySQL / MariaDB | `mysql` | `mysql` | `pymysql` |
| DuckDB | `duckdb` | `duckdb` | `duckdb-engine` |
| SQLite | `sqlite` | — | stdlib |

Aliases: `postgresql`, `pg`, `sqlserver`, `azuresql`, `mariadb`.

`nexassure connectors` shows which drivers are installed on this machine.

## Common fields

```yaml
connections:
  - name: prod                 # referenced by suites
    type: postgres
    host: db.example.com
    port: 5432
    database: analytics
    schema: public             # default schema for unqualified table names
    username: ${env:PGUSER}
    password: ${env:PGPASSWORD}

    readonly: true             # default. Screens every statement
    connect_timeout: 30
    query_timeout: 600         # per-statement, pushed to the server
    pool_size: 5
    max_overflow: 5

    params: {}                 # extra URL query parameters
    connect_args: {}           # extra driver keyword arguments
    tags: [production]
```

Any connection can instead be given a raw SQLAlchemy URL, which overrides every
discrete field:

```yaml
  - name: prod
    type: postgres
    dsn: ${env:DATABASE_URL}
```

---

## Snowflake

```bash
pip install "nexassure[snowflake]"
```

```yaml
  - name: prod_snowflake
    type: snowflake
    account: xy12345.eu-west-1        # required
    username: ${env:SNOWFLAKE_USER}
    password: ${env:SNOWFLAKE_PASSWORD}
    warehouse: ANALYTICS_WH
    database: PROD
    schema: PUBLIC
    role: DATA_QUALITY_READER
```

**Key-pair authentication** — set `private_key_path` and put the passphrase in
`SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`, so it never appears in a config file:

```yaml
    private_key_path: /secrets/snowflake_key.p8
```

**SSO / external browser:**

```yaml
    authenticator: externalbrowser
```

Behaviour worth knowing:

- **Unquoted identifiers fold to UPPER CASE.** Write `ORDERS`, not `orders`, in
  suite files, or quote them.
- `STATEMENT_TIMEOUT_IN_SECONDS` is set from `query_timeout` on every session.
- `REGEXP_LIKE` anchors implicitly. A partial-match pattern needs `.*` on both
  ends.
- `APPROX_COUNT_DISTINCT` is available, so profiling large tables is cheap.
- The warehouse must be running, or the first query pays the resume cost. Give
  NexAssure a small dedicated warehouse with auto-suspend.

A minimal role:

```sql
CREATE ROLE data_quality_reader;
GRANT USAGE ON WAREHOUSE analytics_wh TO ROLE data_quality_reader;
GRANT USAGE ON DATABASE prod TO ROLE data_quality_reader;
GRANT USAGE ON ALL SCHEMAS IN DATABASE prod TO ROLE data_quality_reader;
GRANT SELECT ON ALL TABLES IN DATABASE prod TO ROLE data_quality_reader;
GRANT SELECT ON FUTURE TABLES IN DATABASE prod TO ROLE data_quality_reader;
```

---

## PostgreSQL

```bash
pip install "nexassure[postgres]"
```

```yaml
  - name: prod_postgres
    type: postgres
    host: ${env:PGHOST}
    port: 5432
    database: analytics
    schema: public
    username: ${env:PGUSER}
    password: ${env:PGPASSWORD}
    params:
      sslmode: require
```

- `search_path` is set from `schema`, so unqualified names resolve without
  rewriting every query.
- `statement_timeout` is set from `query_timeout`.
- Regex uses the `~` operator, which is far cheaper than `REGEXP_LIKE` and
  works on every version (`REGEXP_LIKE` only arrived in PG 15).

```sql
CREATE ROLE data_quality_reader LOGIN PASSWORD 'x';
GRANT CONNECT ON DATABASE analytics TO data_quality_reader;
GRANT USAGE ON SCHEMA public TO data_quality_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO data_quality_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO data_quality_reader;
```

---

## SQL Server and Azure SQL

```bash
pip install "nexassure[mssql]"
```

You also need a Microsoft ODBC driver, which is not installable from PyPI:

```bash
# Debian/Ubuntu
curl https://packages.microsoft.com/keys/microsoft.asc | sudo apt-key add -
curl https://packages.microsoft.com/config/ubuntu/22.04/prod.list \
  | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt-get update && sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18

# macOS
brew install msodbcsql18
```

```yaml
  - name: prod_mssql
    type: mssql
    host: sqlserver.example.com
    port: 1433
    database: Analytics
    schema: dbo
    username: ${env:MSSQL_USER}
    password: ${env:MSSQL_PASSWORD}
    driver: ODBC Driver 18 for SQL Server   # auto-detected if omitted
    params:
      Encrypt: "yes"
      TrustServerCertificate: "yes"          # for self-signed on-prem certs
```

T-SQL differences NexAssure handles for you, and the one it cannot:

- Identifiers quote with `[brackets]`; `TOP n` replaces `LIMIT n`; `LEN`
  replaces `LENGTH`; `STDEV` replaces `STDDEV_SAMP`.
- Sessions run at `READ UNCOMMITTED`, so profiling does not take shared locks
  on tables OLTP writers are using.
- **There is no regex engine.** `regex` checks fall back to `LIKE`, so anchored
  wildcard patterns work and richer expressions do not. Write those as
  `custom_sql` on T-SQL engines.

Driver 18 defaults to `Encrypt=yes` and then rejects self-signed certificates,
which is the most common first-connection failure. NexAssure sets
`TrustServerCertificate=yes` by default for on-prem instances; set it to `"no"`
for Azure SQL, which presents a valid certificate.

---

## Azure Synapse

```bash
pip install "nexassure[synapse]"
```

```yaml
  - name: prod_synapse
    type: synapse
    host: myworkspace.sql.azuresynapse.net
    port: 1433
    database: analytics_pool
    schema: dbo
    username: ${env:SYNAPSE_USER}
    password: ${env:SYNAPSE_PASSWORD}
```

**Azure AD authentication:**

```yaml
    authenticator: ActiveDirectoryMsi          # managed identity
    # ActiveDirectoryServicePrincipal, ActiveDirectoryPassword, ActiveDirectoryInteractive
```

Synapse speaks T-SQL over the same driver as SQL Server, but the dedicated SQL
pool is a distributed MPP engine with real restrictions, which the dialect
encodes:

- No `TABLESAMPLE`, so sampling uses a `LIMIT` subquery.
- No `APPROX_COUNT_DISTINCT` on dedicated pools; exact `COUNT(DISTINCT ...)` is
  used instead, which is slower on wide fact tables.
- `PERCENTILE_CONT` only in its windowed form, so percentiles are best-effort.
- Table discovery reads `sys.objects` directly, because the SQLAlchemy
  inspector issues several round trips per call and Synapse is slow at those.

`TrustServerCertificate` defaults to `"no"` here — Synapse endpoints always
present a valid certificate, so there is no reason to relax verification.

---

## Amazon Redshift

```bash
pip install "nexassure[redshift]"
```

```yaml
  - name: prod_redshift
    type: redshift
    host: cluster.abc123.eu-west-1.redshift.amazonaws.com
    port: 5439
    database: analytics
    schema: public
    username: ${env:REDSHIFT_USER}
    password: ${env:REDSHIFT_PASSWORD}
```

Redshift Serverless works with the same settings; point `host` at the workgroup
endpoint.

Redshift forked from PostgreSQL 8.0, so the syntax looks familiar but several
modern functions are missing:

- `APPROXIMATE COUNT(DISTINCT ...)` uses HyperLogLog, turning a full-table
  distinct on a billion-row fact table into a cheap sketch.
- No `TABLESAMPLE`.
- `DATEDIFF(second, ...)` is used for freshness rather than `EXTRACT(EPOCH ...)`.
- `VARCHAR(65535)` is the widest cast available.

---

## Oracle

```bash
pip install "nexassure[oracle]"
```

`python-oracledb` runs in thin mode by default, so no Oracle Instant Client
installation is required.

```yaml
  - name: prod_oracle
    type: oracle
    host: oracle.example.com
    port: 1521
    service_name: ORCLPDB1          # required (or use `database`)
    schema: ANALYTICS
    username: ${env:ORACLE_USER}
    password: ${env:ORACLE_PASSWORD}
```

Oracle is the most divergent engine supported, and these differences change
what checks mean:

- **The empty string and NULL are the same value.** `not_blank` and `not_null`
  are therefore equivalent on Oracle. This is Oracle behaviour, not a bug.
- **Unquoted identifiers fold to UPPER CASE.** Write `ORDERS`, not `orders`.
- No `LIMIT`. `FETCH FIRST n ROWS ONLY` is used throughout, which requires 12c
  or later.
- Every `SELECT` needs a `FROM`, so the connection probe is `SELECT 1 FROM DUAL`.
- `schema` sets `CURRENT_SCHEMA` for the session — Oracle calls a schema a user.
- Schema listing reads `ALL_TABLES` rather than `ALL_USERS`, because the latter
  includes dozens of Oracle-internal accounts.

Autonomous Database works; supply the wallet through `connect_args`.

---

## MySQL and MariaDB

```bash
pip install "nexassure[mysql]"
```

```yaml
  - name: prod_mysql
    type: mysql
    host: mysql.example.com
    port: 3306
    database: analytics
    username: ${env:MYSQL_USER}
    password: ${env:MYSQL_PASSWORD}
```

- Identifiers quote with backticks.
- `max_execution_time` is set from `query_timeout`.
- Percentiles are unavailable before MySQL 8.0.2, so `--percentiles` is
  best-effort.
- MySQL has no separate schema concept; `database` and `schema` are the same
  thing, and either field works.

---

## DuckDB

```bash
pip install "nexassure[duckdb]"
```

```yaml
  - name: local
    type: duckdb
    database: ./warehouse.duckdb    # a file path; omit for in-memory
```

The local-development and CI target: no server, no credentials. It also reads
Parquet and CSV directly, so you can profile a file in a data lake without a
warehouse in the loop:

```yaml
  - name: no_nulls_in_the_parquet
    type: custom_sql
    query: SELECT COUNT(*) FROM read_parquet('s3://bucket/orders/*.parquet') WHERE id IS NULL
    expect: {operator: eq, value: 0}
```

Because DuckDB allows one connection per file, NexAssure shares a single
connection and **serialises** access to it. Checks still all run, they just
queue rather than executing in parallel — the right trade for a local engine.

---

## SQLite

Ships with Python, so no extra is needed. It is the default metastore backend
and a convenient target for tutorials.

```yaml
  - name: local
    type: sqlite
    database: ./warehouse.db
```

Limitations, encoded as dialect flags so checks degrade rather than crash:

- No `STDDEV` — profiling computes it from raw moments instead.
- No percentiles.
- No regex without a registered user function; `regex` checks fall back to
  `GLOB`.

---

## Read-only enforcement

Every connection defaults to `readonly: true`. Each statement is screened
before it reaches a driver: only `SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `EXPLAIN`
and `VALUES` may lead, write keywords outside string literals are rejected, and
multi-statement SQL is refused.

This catches mistakes. It is **not** a substitute for permissions — see
[SECURITY.md](https://github.com/sumit-gupta03/nexassure/blob/main/SECURITY.md). Run NexAssure as a role that only holds `SELECT`.

To opt out for one connection:

```yaml
    readonly: false
```

or for one check:

```yaml
    params:
      allow_write: true
```

## Adding an engine

Connectors resolve through an entry point, so you can ship one from your own
package with no fork:

```toml
[project.entry-points."nexassure.connectors"]
clickhouse = "nexassure_clickhouse:ClickHouseConnector"
```

See [CONTRIBUTING.md](https://github.com/sumit-gupta03/nexassure/blob/main/CONTRIBUTING.md#adding-a-warehouse).
