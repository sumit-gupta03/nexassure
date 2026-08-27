# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""The read-only SQL guard.

This guard is the security boundary for the MCP server and the REST API, so the
tests cover evasion attempts as well as ordinary statements - and, just as
importantly, cover the false positives that would make the guard unusable.
"""

from __future__ import annotations

import pytest

from nexassure.exceptions import UnsafeSQLError
from nexassure.utils.sqlsafe import assert_readonly, is_readonly, split_statements, strip_sql_noise


class TestAllowedStatements:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "select * from orders where total > 0",
            "WITH recent AS (SELECT * FROM orders) SELECT COUNT(*) FROM recent",
            "SHOW TABLES",
            "DESCRIBE orders",
            "EXPLAIN SELECT * FROM orders",
            "  SELECT 1  ",
            "SELECT 1;",
        ],
    )
    def test_reads_are_allowed(self, sql):
        assert is_readonly(sql)


class TestBlockedStatements:
    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE orders",
            "DELETE FROM orders",
            "UPDATE orders SET total = 0",
            "INSERT INTO orders VALUES (1)",
            "TRUNCATE TABLE orders",
            "ALTER TABLE orders ADD COLUMN x INT",
            "GRANT SELECT ON orders TO public",
            "CREATE TABLE t (id INT)",
            "MERGE INTO orders USING staging ON 1=1",
        ],
    )
    def test_writes_are_rejected(self, sql):
        assert not is_readonly(sql)
        with pytest.raises(UnsafeSQLError):
            assert_readonly(sql)


class TestEvasion:
    def test_a_write_smuggled_after_a_select_is_rejected(self):
        with pytest.raises(UnsafeSQLError, match="single statement"):
            assert_readonly("SELECT 1; DROP TABLE orders")

    def test_a_write_inside_a_cte_is_rejected(self):
        with pytest.raises(UnsafeSQLError):
            assert_readonly("WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x")

    def test_a_comment_cannot_hide_a_write(self):
        with pytest.raises(UnsafeSQLError):
            assert_readonly("SELECT 1 /* harmless */ ; DELETE FROM orders")

    def test_multi_statement_can_be_opted_into(self):
        assert_readonly("SELECT 1; SELECT 2", allow_multiple=True)


class TestFalsePositives:
    """A guard that blocks legitimate reads is worse than no guard at all."""

    def test_a_write_keyword_inside_a_string_literal_is_fine(self):
        assert is_readonly("SELECT * FROM audit WHERE action = 'delete'")

    def test_a_column_named_like_a_keyword_is_fine(self):
        assert is_readonly('SELECT "drop", "update" FROM legacy_table')

    def test_a_keyword_inside_a_comment_is_fine(self):
        assert is_readonly("SELECT 1 -- we used to DROP this table")

    def test_an_escaped_quote_does_not_break_parsing(self):
        assert is_readonly("SELECT * FROM t WHERE name = 'O''Brien'")


class TestHelpers:
    def test_strip_removes_comments_and_literals(self):
        cleaned = strip_sql_noise("SELECT 'delete' /* drop */ -- update\nFROM t")
        assert "delete" not in cleaned
        assert "drop" not in cleaned
        assert "update" not in cleaned
        assert "FROM t" in cleaned

    def test_split_ignores_semicolons_inside_literals(self):
        assert split_statements("SELECT ';' FROM t; SELECT 2") == ["SELECT ';' FROM t", "SELECT 2"]

    def test_empty_sql_is_rejected(self):
        with pytest.raises(UnsafeSQLError):
            assert_readonly("   ")
