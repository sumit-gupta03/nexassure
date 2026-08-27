# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Shared utilities."""

from .sqlsafe import assert_readonly, is_readonly, split_statements

__all__ = ["assert_readonly", "is_readonly", "split_statements"]
