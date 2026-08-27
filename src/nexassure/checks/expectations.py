# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Expectation evaluation.

Turns a query result plus an :class:`~nexassure.core.models.Expectation` into a
verdict. This is the half of NexAssure that makes "here is my SQL and here is the
answer I expect" a first-class test.

Two steps:

1. **Reduce** - collapse the result set according to ``shape`` (a scalar, one
   row, one column, the whole table, or just its row count).
2. **Compare** - apply ``operator`` to the reduction and ``value``.

Comparison is deliberately forgiving about types that databases return
inconsistently. ``Decimal("3")``, ``3`` and ``3.0`` compare equal; a ``date``
compares equal to the ISO string a user typed in YAML. Being strict there just
produces failures that teach people to distrust the tool.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from ..connectors.base import QueryResult
from ..core.enums import Operator, ResultShape
from ..core.models import Expectation
from ..exceptions import ExpectationError

#: Values treated as "no data" when reducing a result set.
_EMPTY = object()


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def _as_number(value: Any) -> float | None:
    """Best-effort numeric coercion; ``None`` when the value is not numeric."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_comparable(value: Any) -> Any:
    """Coerce a value into something that compares sensibly across drivers.

    Numbers become floats, temporal values become ISO strings, everything else
    is left alone. Applied to both sides of every comparison so that a Decimal
    from Oracle and an int from YAML land on common ground.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        try:
            return float(value)
        except (InvalidOperation, ValueError):
            return str(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return value


def _loose_equal(left: Any, right: Any, exp: Expectation) -> bool:
    """Equality that tolerates driver type drift and, optionally, tolerance."""
    a, b = _as_comparable(left), _as_comparable(right)
    if a is None or b is None:
        return a is b or a == b

    na, nb = _as_number(a), _as_number(b)
    if na is not None and nb is not None:
        if exp.tolerance or exp.relative_tolerance:
            return math.isclose(na, nb, abs_tol=exp.tolerance, rel_tol=exp.relative_tolerance)
        return na == nb

    if isinstance(a, str) and isinstance(b, str):
        if exp.ignore_case:
            return a.strip().lower() == b.strip().lower()
        return a.strip() == b.strip()

    return a == b


def _rows_as_tuples(result: QueryResult) -> list[tuple[Any, ...]]:
    return [tuple(_as_comparable(v) for v in row) for row in result.rows]


def _sortable(row: tuple[Any, ...]) -> tuple[str, ...]:
    """Total ordering key. Rows can mix types, so compare their reprs."""
    return tuple("" if v is None else repr(v) for v in row)


# --------------------------------------------------------------------------- #
# Reduction
# --------------------------------------------------------------------------- #


def reduce_result(result: QueryResult, shape: ResultShape) -> Any:
    """Collapse a result set according to ``shape``."""
    if shape is ResultShape.ROW_COUNT:
        return result.row_count

    if shape is ResultShape.SCALAR:
        if result.is_empty():
            return _EMPTY
        first = result.first()
        if first is None or not first:
            return _EMPTY
        return first[0]

    if shape is ResultShape.ROW:
        if result.is_empty():
            return _EMPTY
        return list(result.rows[0])

    if shape is ResultShape.COLUMN:
        return result.column_values(0)

    if shape is ResultShape.TABLE:
        return [list(row) for row in result.rows]

    raise ExpectationError(f"Unsupported result shape {shape!r}")


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #


def _compare_ordering(op: Operator, observed: Any, expected: Any) -> bool:
    left, right = _as_number(observed), _as_number(expected)
    if left is None or right is None:
        left, right = _as_comparable(observed), _as_comparable(expected)
        try:
            match op:
                case Operator.GT:
                    return left > right
                case Operator.GTE:
                    return left >= right
                case Operator.LT:
                    return left < right
                case Operator.LTE:
                    return left <= right
        except TypeError as exc:
            raise ExpectationError(
                f"Cannot order-compare {observed!r} with {expected!r}: {exc}"
            ) from exc
    match op:
        case Operator.GT:
            return left > right
        case Operator.GTE:
            return left >= right
        case Operator.LT:
            return left < right
        case Operator.LTE:
            return left <= right
    raise ExpectationError(f"{op!r} is not an ordering operator")


def _compare_table(observed: Any, expected: Any, exp: Expectation) -> bool:
    """Compare two row sets, honouring ``ignore_row_order``.

    ``expected`` may be a list of lists or a list of dicts. Dicts are converted
    positionally by sorted key, which keeps YAML readable without forcing the
    author to remember the select-list order.
    """
    if not isinstance(observed, list):
        raise ExpectationError("Table comparison needs shape 'table' or 'column'")

    def normalise(rows: Any) -> list[tuple[Any, ...]]:
        out: list[tuple[Any, ...]] = []
        for row in rows or []:
            if isinstance(row, dict):
                out.append(tuple(_as_comparable(row[k]) for k in sorted(row)))
            elif isinstance(row, (list, tuple)):
                out.append(tuple(_as_comparable(v) for v in row))
            else:
                out.append((_as_comparable(row),))
        return out

    left, right = normalise(observed), normalise(expected)
    if exp.ignore_row_order:
        return sorted(left, key=_sortable) == sorted(right, key=_sortable)
    return left == right


def compare(observed: Any, exp: Expectation) -> tuple[bool, str]:
    """Apply ``exp.operator`` to ``observed``.

    Returns:
        ``(passed, human_readable_explanation)``.
    """
    op, expected = exp.operator, exp.value
    missing = observed is _EMPTY
    shown = "<no rows>" if missing else observed

    match op:
        case Operator.IS_NULL:
            return (missing or observed is None), f"expected NULL, got {shown!r}"
        case Operator.IS_NOT_NULL:
            ok = not missing and observed is not None
            return ok, f"expected a non-NULL value, got {shown!r}"
        case Operator.EMPTY:
            ok = missing or observed in (None, 0, [], "")
            return ok, f"expected an empty result, got {shown!r}"
        case Operator.NOT_EMPTY:
            ok = not missing and observed not in (None, 0, [], "")
            return ok, f"expected a non-empty result, got {shown!r}"

    if missing:
        return False, f"query returned no rows, expected {expected!r}"

    match op:
        case Operator.EQ:
            return _loose_equal(observed, expected, exp), f"expected {expected!r}, got {observed!r}"
        case Operator.NE:
            ok = not _loose_equal(observed, expected, exp)
            return ok, f"expected anything but {expected!r}, got {observed!r}"
        case Operator.APPROX:
            tol = exp.tolerance or 1e-9
            rel = exp.relative_tolerance
            a, b = _as_number(observed), _as_number(expected)
            if a is None or b is None:
                raise ExpectationError(
                    f"operator 'approx' needs numeric values, got {observed!r} and {expected!r}"
                )
            ok = math.isclose(a, b, abs_tol=tol, rel_tol=rel)
            return ok, f"expected ~{expected!r} (+/-{tol}, rel {rel}), got {observed!r}"
        case Operator.GT | Operator.GTE | Operator.LT | Operator.LTE:
            ok = _compare_ordering(op, observed, expected)
            return ok, f"expected value {op.value} {expected!r}, got {observed!r}"
        case Operator.BETWEEN:
            low, high = expected
            value = _as_number(observed)
            lo, hi = _as_number(low), _as_number(high)
            if value is None or lo is None or hi is None:
                comparable = _as_comparable(observed)
                ok = _as_comparable(low) <= comparable <= _as_comparable(high)
            else:
                ok = lo <= value <= hi
            return ok, f"expected a value between {low!r} and {high!r}, got {observed!r}"
        case Operator.IN:
            candidates = expected if isinstance(expected, (list, tuple, set)) else [expected]
            ok = any(_loose_equal(observed, c, exp) for c in candidates)
            return ok, f"expected one of {list(candidates)!r}, got {observed!r}"
        case Operator.NOT_IN:
            candidates = expected if isinstance(expected, (list, tuple, set)) else [expected]
            ok = not any(_loose_equal(observed, c, exp) for c in candidates)
            return ok, f"expected none of {list(candidates)!r}, got {observed!r}"
        case Operator.MATCHES | Operator.NOT_MATCHES:
            flags = re.IGNORECASE if exp.ignore_case else 0
            try:
                pattern = re.compile(str(expected), flags)
            except re.error as err:
                raise ExpectationError(f"Invalid regex {expected!r}: {err}") from err
            matched = bool(pattern.search(str(observed)))
            ok = matched if op is Operator.MATCHES else not matched
            verb = "match" if op is Operator.MATCHES else "not match"
            return ok, f"expected {observed!r} to {verb} /{expected}/"
        case Operator.CONTAINS:
            haystack = observed if isinstance(observed, (list, tuple)) else str(observed)
            if isinstance(haystack, str):
                needle = str(expected)
                ok = needle.lower() in haystack.lower() if exp.ignore_case else needle in haystack
            else:
                ok = any(_loose_equal(item, expected, exp) for item in haystack)
            return ok, f"expected {observed!r} to contain {expected!r}"
        case Operator.SET_EQUALS:
            left = {
                _sortable((v,)) for v in (observed if isinstance(observed, list) else [observed])
            }
            right = {
                _sortable((v,)) for v in (expected if isinstance(expected, list) else [expected])
            }
            return left == right, f"expected the set {expected!r}, got {observed!r}"
        case Operator.ROWS_EQUAL:
            ok = _compare_table(observed, expected, exp)
            return ok, f"expected {len(expected or [])} row(s) to match exactly"

    raise ExpectationError(f"Unsupported operator {op!r}")


def evaluate(result: QueryResult, exp: Expectation) -> tuple[bool, Any, str]:
    """Reduce ``result`` per ``exp.shape`` then compare it.

    Returns:
        ``(passed, observed_value, explanation)``. ``observed_value`` is the
        reduced value, which is what gets stored on the result and shown in
        reports.
    """
    reduced = reduce_result(result, exp.shape)
    passed, explanation = compare(reduced, exp)
    observed = None if reduced is _EMPTY else reduced
    return passed, observed, explanation
