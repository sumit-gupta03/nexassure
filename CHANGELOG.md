# Changelog

All notable changes to NexAssure are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-08-28

### Fixed

- `--json` output is now plain, pipeable JSON. It previously rendered through
  Rich, which colourises and re-wraps, so `nexassure connectors --json | jq`
  received ANSI escape codes and failed to parse.

### Changed

- Sumit Kumar Gupta and Nitish Pradhan are credited as authors in the package
  metadata, `NOTICE`, `AUTHORS.md`, the README and the documentation.

### Added

- Release workflow publishing to PyPI via Trusted Publishing (OIDC), so no API
  token is stored anywhere.
- MkDocs Material documentation site.


## [0.1.0] - 2026-08-27

First public release.

### Added

- **Connectors** for Snowflake, PostgreSQL, SQL Server, Amazon Redshift, Azure
  Synapse, Oracle, MySQL/MariaDB, DuckDB and SQLite, behind a `Dialect`
  abstraction so one check definition runs unchanged on every engine.
- **Metastore** created automatically on first connect: connections, datasets,
  columns, check definitions, runs, check results, profiles and schedules.
  Defaults to SQLite under `~/.nexassure`; point it at Postgres to share history.
- **Profiling** with batched aggregates: row counts, null ratios, cardinality,
  duplicates, ranges, string lengths, top values, and optional percentiles and
  whole-row duplicate detection.
- **Check types** across completeness, uniqueness, volume, validity,
  timeliness, consistency and statistics, plus `custom_sql`,
  `sql_returns_no_rows`, `sql_returns_rows` and `compare_queries` for
  description + query + expected output rules.
- **Expectations** with `scalar` / `row` / `column` / `table` / `row_count`
  shapes and sixteen comparison operators, tolerant of driver type drift.
- **Parallel execution** with `depends_on` waves, `fail_fast`, per-check
  `severity` and `threshold`.
- **Suggestion engine** that turns a profile into a conservative starter suite.
- **Scheduler** driven by cron expressions, with no overlap and no catch-up.
- **Reporting** to the terminal, JSON, JUnit XML, Markdown and self-contained HTML.
- **MCP server** exposing 19 tools, read-only by default, with bounded output.
- **REST API** with OpenAPI docs and optional bearer-token auth.
- **CLI** with meaningful exit codes: 0 clean, 1 checks failed, 2 tool error.
- **Read-only SQL guard** applied to every user- and agent-supplied statement.
- **Notifications** to Slack and generic webhooks.

[Unreleased]: https://github.com/sumit-gupta03/nexassure/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/sumit-gupta03/nexassure/releases/tag/v0.1.1
[0.1.0]: https://github.com/sumit-gupta03/nexassure/releases/tag/v0.1.0
