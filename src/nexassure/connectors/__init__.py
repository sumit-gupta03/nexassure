# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Warehouse connectors.

Import the registry, not the individual modules: connector classes are resolved
lazily so that installing NexAssure never requires every database driver.
"""

from .base import BaseConnector, Dialect, QueryResult, classify_type
from .registry import (
    available,
    create_connector,
    describe_connectors,
    get_connector_class,
    open_connection,
    register,
)

__all__ = [
    "BaseConnector",
    "Dialect",
    "QueryResult",
    "available",
    "classify_type",
    "create_connector",
    "describe_connectors",
    "get_connector_class",
    "open_connection",
    "register",
]
