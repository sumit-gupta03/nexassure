# Contributing to NexAssure

Thanks for considering a contribution. NexAssure is Apache-2.0 licensed, and the
project is designed so that the most valuable contributions — a new warehouse,
a new check type — can be made without touching the core.

## Getting set up

```bash
git clone https://github.com/sumit-gupta03/nexassure.git
cd nexassure
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,duckdb,mcp,server,notify]"
pytest
```

The whole test suite runs against DuckDB in a temp directory. No credentials,
no containers, no network. If `pytest` does not pass on a clean checkout, that
is a bug — please open an issue.

```bash
make test         # everything
make test-fast    # unit tests only, no database
make lint         # ruff check + format check
make format       # apply formatting
make check        # lint + typecheck + test
make demo         # seed the quickstart warehouse and run its suite
```

## What makes a good pull request

- **One thing at a time.** A new check type and a connector fix are two PRs.
- **A test that fails without your change.** For anything touching generated
  SQL, that means an integration test in `tests/integration/`, not a string
  assertion — SQL that looks right and does not run is the failure mode this
  project exists to prevent.
- **A note in `CHANGELOG.md`** under `## [Unreleased]`.
- **Docstrings that say why.** The codebase documents reasoning, not mechanics.
  `# increment the counter` is noise; `# NOT EXISTS beats NOT IN here: NOT IN
  silently returns zero rows when the parent column contains a NULL` is the
  kind of comment worth writing.

## Adding a check type

Most checks are a few lines, because `RowPredicateCheck` already handles
counting and sampling failing rows. You supply a SQL predicate that is **true
for rows that fail**:

```python
from nexassure.checks import CheckContext, RowPredicateCheck, register_check


@register_check
class EmailLooksValidCheck(RowPredicateCheck):
    """Values look like email addresses."""

    type_name = "email_valid"
    summary = "String values look like email addresses"
    requires_column = True
    violation_noun = "malformed emails"

    def failing_predicate(self, ctx: CheckContext) -> str:
        column = self.col(ctx)
        pattern = ctx.dialect.string_literal(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
        return f"({column} IS NOT NULL AND NOT ({ctx.dialect.regexp_match(column, pattern)}))"
```

Checks that need a `GROUP BY`, a second table, or catalog introspection
subclass `Check` and implement `evaluate()` directly — see `UniqueCheck` and
`ReferentialIntegrityCheck` in `src/nexassure/checks/builtin.py`.

Rules:

- **Never interpolate a value into SQL directly.** Use
  `ctx.dialect.string_literal()` for strings and `ctx.dialect.quote()` for
  identifiers. There is a test asserting a value containing an apostrophe is
  escaped rather than injected; keep it passing.
- **Never hardcode dialect-specific syntax.** Go through `ctx.dialect` so the
  check works on all nine engines. If a fragment you need is missing, add it to
  `Dialect` with an ANSI default and override it where it differs.
- **Declare `supported_params`.** Unknown parameters then fail validation
  offline instead of producing a confusing runtime error.

You can also ship a check type from your own package via an entry point, with
no fork:

```toml
[project.entry-points."nexassure.checks"]
my_checks = "my_package.checks:register"
```

## Adding a warehouse

Subclass `BaseConnector` and `Dialect` in `src/nexassure/connectors/`, then
register it in `src/nexassure/connectors/registry.py` and add an extra in
`pyproject.toml`.

A connector needs to answer three things: how to build a SQLAlchemy URL, how
its dialect differs from ANSI, and — if `database` is not a SQL catalog name
(as on DuckDB and SQLite) — override `catalog_name` to return `None`.

Look at `postgres.py` for the simplest case and `synapse.py` for one with real
engine restrictions. Encode limitations as dialect flags
(`supports_percentile`, `supports_regexp`, `limit_style`) rather than letting
checks fail at runtime with a parser error.

CI cannot reach a real Snowflake or Oracle, so connector PRs should say which
engine and version you tested against by hand.

## Reporting bugs

Include the NexAssure version (`nexassure version`), the warehouse and its version,
the suite YAML that reproduces it, and the output of the failing command with
`--verbose`. If the problem is generated SQL, `nexassure run --show-sql` prints
the exact statement.

## Security

Please do not open a public issue for a security problem — see
[SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## Licensing

By contributing, you agree that your contributions are licensed under the
Apache License 2.0, per section 5 of the licence. New files should carry the
standard header:

```python
# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
```
