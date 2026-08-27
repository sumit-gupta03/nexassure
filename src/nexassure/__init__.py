# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""NexAssure - open-source data testing for the modern warehouse.

Profile tables, declare checks in YAML, run them in parallel against Snowflake,
PostgreSQL, SQL Server, Redshift, Synapse, Oracle and more, and keep the whole
history in a metastore that is created for you on first connect.

Quick start::

    from nexassure import NexAssure

    with NexAssure() as na:
        run = na.run_suite("orders_quality")
        print(run.summary)

See https://github.com/sumit-gupta03/nexassure for documentation.
"""

from __future__ import annotations

from .api import NexAssure
from .config import ProjectConfig, connection_from_dsn, load_config
from .core.enums import CheckStatus, ColumnKind, Operator, ResultShape, RunStatus, Severity
from .core.models import (
    CheckResult,
    CheckSpec,
    ConnectionConfig,
    Expectation,
    RunResult,
    Suite,
    TableProfile,
    TableRef,
)
from .exceptions import (
    CheckExecutionError,
    ConfigError,
    ConnectionError_,
    DriverNotInstalled,
    NexAssureError,
    SuiteError,
    UnsafeSQLError,
)
from .version import VERSION, __version__

__all__ = [
    "VERSION",
    "CheckExecutionError",
    "CheckResult",
    "CheckSpec",
    "CheckStatus",
    "ColumnKind",
    "ConfigError",
    "ConnectionConfig",
    "ConnectionError_",
    "DriverNotInstalled",
    "Expectation",
    "NexAssure",
    "NexAssureError",
    "Operator",
    "ProjectConfig",
    "ResultShape",
    "RunResult",
    "RunStatus",
    "Severity",
    "Suite",
    "SuiteError",
    "TableProfile",
    "TableRef",
    "UnsafeSQLError",
    "__version__",
    "connection_from_dsn",
    "load_config",
]
