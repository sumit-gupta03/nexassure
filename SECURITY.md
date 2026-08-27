# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue. Report privately through
[GitHub Security Advisories](https://github.com/sumit-gupta03/nexassure/security/advisories/new).

Include what an attacker can do, the steps to reproduce, and the affected
version. You can expect an acknowledgement within three working days and an
assessment within ten.

## Supported versions

Until 1.0, security fixes land on the latest minor release only.

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Security model

Understanding what NexAssure does and does not protect against will tell you
whether something is a vulnerability or expected behaviour.

### NexAssure executes SQL that people and agents write

That is the product. Suite YAML is trusted configuration, in the same category
as application source: anyone who can edit a suite file can run the SQL in it.
Treat suite files with the same review discipline as code.

### The read-only guard

Connections default to `readonly: true`. Every statement is screened by
`nexassure.utils.sqlsafe` before it reaches a driver: only `SELECT`, `WITH`,
`SHOW`, `DESCRIBE`, `EXPLAIN` and `VALUES` may lead; write keywords are
rejected when they appear outside string literals, quoted identifiers and
comments; and multi-statement SQL is refused, because a trailing statement is
the classic way to smuggle a write past a leading `SELECT`.

**This is defence in depth, not a security boundary.** It is a keyword screen,
not a SQL parser, and a determined attacker who can already write arbitrary SQL
into your config has other options. The actual boundary is database
permissions: **run NexAssure as a role that only holds `SELECT`.** If you do that,
the guard is a convenience that catches mistakes; if you do not, you are
relying on a keyword screen, which is not a position to be in.

A confirmed bypass of the guard is still a valid report — please send it.

### Credentials

- Never written to the metastore. `nexassure_connections` stores host, port, database
  and schema, never a password or key.
- `ConnectionConfig.safe_summary` redacts secrets, and it is what the REST API
  and MCP server return.
- Config files should use `${env:VAR}` references so secrets live in the
  environment or a secret manager rather than in git.
- Metastore URLs are rendered with `hide_password=True` in logs.

A credential appearing in a log line, an API response, an MCP tool result or a
report is a vulnerability. Please report it.

### The MCP server

Read-only by default. File-writing tools are only registered under
`--allow-writes`. Every SQL path uses the same guard as the CLI, so an agent
cannot drop a table through it. Tool output is capped so a single call cannot
exhaust a context window.

The MCP server inherits whatever database permissions its connections carry.
Point it at a read-only role.

### The REST API

Unauthenticated by default and intended to run inside a trusted network or
behind a gateway that owns authentication. Setting `NEXASSURE_API_TOKEN` enables a
bearer-token check using a constant-time comparison. `/health` and `/ready`
stay open so orchestrators can probe them.

The API has no authorisation model: any caller who can authenticate can query
every configured connection. Do not expose it to the public internet.

### Sample rows

When a check fails, NexAssure captures a few offending rows so the failure is
actionable. **Those rows are real data**, and they are written to the metastore
and included in reports, notifications and API responses. On a table holding
personal or regulated data, set `sample_limit: 0` on the check or in
`defaults:` to turn that off.

## Out of scope

- Denial of service through a deliberately expensive query in a suite file.
  Suite YAML is trusted input; use warehouse query timeouts and resource
  monitors.
- Vulnerabilities in database drivers. Report those upstream, and please tell
  us so we can pin around them.
- The read-only guard failing to block SQL on a connection where you set
  `readonly: false` or a check where you set `allow_write: true`. Those are
  explicit opt-outs.
