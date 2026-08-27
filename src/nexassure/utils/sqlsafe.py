# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Read-only SQL guard.

NexAssure executes SQL that users write in YAML and that agents write over MCP.
Connections default to ``readonly: true``, and every statement is screened here
before it reaches a driver.

This is a defence-in-depth layer, not a substitute for database permissions.
The right way to run NexAssure against production is with a role that only holds
SELECT. The guard exists so that a typo or a confused agent cannot drop a table
even when someone has pointed NexAssure at an over-privileged account.
"""

from __future__ import annotations

import re

from ..exceptions import UnsafeSQLError

#: Statements that may run on a read-only connection.
_ALLOWED_LEADERS = frozenset({"select", "with", "show", "describe", "desc", "explain", "values"})

#: Keywords that mutate data or schema. Matched as whole words outside strings.
_FORBIDDEN = frozenset(
    {
        "insert",
        "update",
        "delete",
        "merge",
        "upsert",
        "truncate",
        "drop",
        "alter",
        "create",
        "replace",
        "grant",
        "revoke",
        "rename",
        "comment",
        "call",
        "exec",
        "execute",
        "copy",
        "unload",
        "vacuum",
        "analyze",
        "attach",
        "detach",
        "load",
        "install",
        "set",
        "reset",
        "begin",
        "commit",
        "rollback",
        "savepoint",
        "lock",
        "shutdown",
        "kill",
        "use",
    }
)

#: ``CREATE`` is allowed in these positions because they are session-scoped and harmless.
_ALLOWED_PHRASES = (
    "create temporary view",
    "create temp view",
    "create or replace temporary view",
)

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_DQ_IDENTIFIER = re.compile(r'"(?:[^"]|"")*"')
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def strip_sql_noise(sql: str) -> str:
    """Remove comments, string literals and quoted identifiers.

    What is left is the bare skeleton of the statement, so keyword matching does
    not trip over a column literally named ``"drop"`` or the word ``delete``
    inside a message string.
    """
    without_comments = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql))
    without_strings = _STRING_LITERAL.sub("''", without_comments)
    return _DQ_IDENTIFIER.sub('""', without_strings)


def split_statements(sql: str) -> list[str]:
    """Split on semicolons that sit outside strings, comments and quoted identifiers."""
    statements: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            current.append(sql[i : j + 1])
            i = j + 1
        elif ch == '"':
            j = sql.find('"', i + 1)
            j = n - 1 if j == -1 else j
            current.append(sql[i : j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif ch == ";":
            statements.append("".join(current))
            current = []
            i += 1
        else:
            current.append(ch)
            i += 1
    statements.append("".join(current))
    return [s.strip() for s in statements if s.strip()]


def assert_readonly(sql: str, *, allow_multiple: bool = False) -> None:
    """Raise :class:`UnsafeSQLError` unless every statement only reads.

    Args:
        sql: The raw statement text supplied by a suite or an MCP caller.
        allow_multiple: Permit several semicolon-separated statements. Off by
            default, because a trailing statement is the classic way to smuggle
            a write past a leading SELECT.
    """
    statements = split_statements(sql)
    if not statements:
        raise UnsafeSQLError("Empty SQL statement")
    if len(statements) > 1 and not allow_multiple:
        raise UnsafeSQLError(
            f"Expected a single statement, found {len(statements)}. "
            "Multi-statement SQL is rejected on read-only connections.",
            statement_count=len(statements),
        )

    for statement in statements:
        skeleton = strip_sql_noise(statement).lower()
        words = _WORD.findall(skeleton)
        if not words:
            raise UnsafeSQLError("Statement contains no SQL keywords", sql=statement[:200])

        if words[0] not in _ALLOWED_LEADERS:
            raise UnsafeSQLError(
                f"Statement starts with {words[0]!r}; read-only connections allow only "
                f"{', '.join(sorted(_ALLOWED_LEADERS))}.",
                keyword=words[0],
                sql=statement[:200],
            )

        collapsed = " ".join(words)
        for phrase in _ALLOWED_PHRASES:
            collapsed = collapsed.replace(phrase, " ")

        remaining = set(collapsed.split())
        offending = remaining & _FORBIDDEN
        if offending:
            raise UnsafeSQLError(
                f"Statement contains write keyword(s): {', '.join(sorted(offending))}. "
                "Set 'readonly: false' on the connection if this is intentional.",
                keywords=sorted(offending),
                sql=statement[:200],
            )


def is_readonly(sql: str, *, allow_multiple: bool = False) -> bool:
    """Non-raising form of :func:`assert_readonly`."""
    try:
        assert_readonly(sql, allow_multiple=allow_multiple)
    except UnsafeSQLError:
        return False
    return True
