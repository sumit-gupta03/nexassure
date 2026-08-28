# MCP server

NexAssure ships a Model Context Protocol server, so an AI agent can explore your
warehouse, profile it, propose checks grounded in that profile, and run them —
without you handing it write access to anything.

```bash
pip install "nexassure[mcp]"
nexassure mcp
```

## Connecting a client

Most desktop MCP clients read a JSON config:

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

For Claude Code:

```bash
claude mcp add nexassure -- nexassure mcp --config /path/to/nexassure.yml
```

Transports:

```bash
nexassure mcp                                   # stdio (default, for desktop clients)
nexassure mcp --transport http --port 8081      # networked
nexassure mcp --transport sse --port 8081       # older clients
```

Works with both the 1.x and 2.x Python MCP SDKs — `FastMCP` was renamed
`MCPServer` in 2.0, and NexAssure resolves whichever is installed.

## The loop it is designed for

```
list_connections → list_tables → profile_table → suggest_checks → run_check → run_suite
```

That is the sequence a data engineer performs by hand. The server instructions
tell the model to follow it, and in particular to prefer `profile_table` over a
scatter of ad-hoc `COUNT(*)` queries: profiling is one batched pass that returns
nulls, cardinality, ranges and top values together.

## Tools

### Orientation

| Tool | Purpose |
|---|---|
| `nexassure_info` | Version, project, connections, suites, installed connectors, check types |
| `list_check_types` | Every check type with its parameters — call before writing a suite |

### Exploring

| Tool | Purpose |
|---|---|
| `list_connections` | Configured data sources, credentials redacted |
| `test_connection` | Verify one works, with latency and server version |
| `list_schemas` | Schemas on a connection |
| `list_tables` | Tables and views, fully qualified |
| `describe_table` | Columns, types, nullability, keys |
| `discover_catalog` | Walk the catalog into the metastore |

### Understanding

| Tool | Purpose |
|---|---|
| `profile_table` | Row count, nulls, cardinality, ranges, lengths, top values |
| `suggest_checks` | A starter suite grounded in a real profile, as YAML |

`profile_table` takes `sample_rows`, `include_percentiles`,
`include_duplicate_rows` and `where`, so an agent can profile a billion-row
table cheaply and then drill in.

### Testing

| Tool | Purpose |
|---|---|
| `run_check` | One ad-hoc check, no file needed |
| `run_suite` | Execute a suite, with `select` / `tags` / `dry_run` |
| `list_suites` | Suites in the project |
| `validate_suites` | Lint offline, without touching a database |
| `run_query` | Read-only SQL |

`run_check` is the important one for exploration: it lets an agent test a
hypothesis about the data and get a structured verdict, without writing a file.

```json
{
  "connection": "prod",
  "check_type": "custom_sql",
  "name": "orphaned_orders",
  "description": "Orders whose customer_id has no matching customer",
  "query": "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.id = o.customer_id)",
  "expect": {"operator": "eq", "value": 0}
}
```

### Triage

| Tool | Purpose |
|---|---|
| `quality_summary` | Headline numbers over a window |
| `recent_failures` | What failed recently, newest first |
| `run_history` | Recent runs and outcomes |
| `get_run` | One run with all its results |

### Writing (opt-in)

| Tool | Purpose |
|---|---|
| `save_suite` | Write suite YAML to a file, then validate it |

Only registered under `--allow-writes`.

## Resources

| URI | Content |
|---|---|
| `nexassure://catalog` | Every catalogued dataset, as JSON |
| `nexassure://checks` | The check-type catalog, as JSON |

## Safety

These are the properties that make it reasonable to point this at a real
warehouse.

**Read-only by default.** Every SQL path — `run_query`, `custom_sql` checks,
generated check SQL — goes through the same guard the CLI uses: only `SELECT`,
`WITH`, `SHOW`, `DESCRIBE`, `EXPLAIN` and `VALUES` may lead; write keywords
outside string literals are rejected; multi-statement SQL is refused, because a
trailing statement is the classic way to smuggle a write past a leading
`SELECT`. An agent cannot drop a table through this server.

**This is defence in depth, not the boundary.** Point the server at a database
role that only holds `SELECT`. See [SECURITY.md](https://github.com/sumit-gupta03/nexassure/blob/main/SECURITY.md).

**Bounded output.** Queries cap at 200 rows, profiles at 200 columns, and long
values are truncated with a note. One tool call cannot flood a context window,
and an agent that asks for a whole table gets a clear truncation flag rather
than a silently partial answer.

**Errors are values.** A failing tool returns `{"ok": false, "error": "...",
"code": "..."}` rather than raising a protocol error, so the model can read the
reason and adapt — usually by fixing its own arguments.

**File writes are opt-in.** `save_suite` only exists under `--allow-writes`.

**Credentials never appear.** `list_connections` returns redacted summaries;
there is no tool that returns a password.

## Worked example

> **Ask:** "Have a look at the orders table in our warehouse and tell me what
> data quality problems it has."

An agent with this server typically:

1. `nexassure_info` — sees `prod` is configured
2. `list_tables` with `schema: PUBLIC` — finds `PROD.PUBLIC.ORDERS`
3. `profile_table` — 1.28M rows; `total` has 1,204 NULLs and a minimum of
   −49.99; `status` has 4 distinct values
4. `run_check` with `type: range, params: {min: 0}` — confirms 312 negative
   totals
5. `run_check` with `type: referential_integrity` — finds 47 orphaned orders
6. `suggest_checks` — returns a starter suite
7. Reports the three real problems, with counts, and offers the YAML

Every step is read-only. The output is a diagnosis backed by numbers rather than
a guess from column names.

## Troubleshooting

**Tools do not appear.** Confirm `pip install "nexassure[mcp]"` and that
`nexassure mcp` starts without error. Logs go to stderr; stdout belongs to the
protocol, so anything printed there corrupts the stream.

**"Unknown connection".** The server resolves `nexassure.yml` by searching upward
from its working directory, which is set by the client, not by you. Pass
`--config` with an absolute path.

**Environment variables are unset.** Desktop clients often launch the server
with a minimal environment, so `${env:SNOWFLAKE_PASSWORD}` resolves to nothing.
Set them in the client config, or use `${env:VAR:-default}` where a default is
safe.

**Writes are refused.** That is the guard working. If it is genuinely
intentional, set `readonly: false` on the connection — and consider whether an
agent should have that.
