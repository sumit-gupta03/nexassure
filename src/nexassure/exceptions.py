# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for NexAssure.

Every error raised by NexAssure derives from :class:`NexAssureError`, so embedders can
catch a single base class.  Errors carry a stable ``code`` used by the REST API
and the MCP server to give machine-readable failures.
"""

from __future__ import annotations

from typing import Any


class NexAssureError(Exception):
    """Base class for all NexAssure errors."""

    code = "nexassure_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}


class ConfigError(NexAssureError):
    """Malformed or missing configuration."""

    code = "config_error"


class ConnectionError_(NexAssureError):
    """Could not reach or authenticate against a data source."""

    code = "connection_error"


class DriverNotInstalled(ConnectionError_):
    """The optional driver extra for a connector is not installed."""

    code = "driver_not_installed"

    def __init__(self, connector: str, extra: str, original: Exception | None = None) -> None:
        super().__init__(
            f"The {connector!r} connector needs an extra driver. "
            f"Install it with:  pip install 'nexassure[{extra}]'",
            connector=connector,
            extra=extra,
            original=str(original) if original else None,
        )


class UnknownConnector(ConfigError):
    code = "unknown_connector"


class UnknownCheckType(ConfigError):
    code = "unknown_check_type"


class SuiteError(ConfigError):
    """A suite file failed to load or validate."""

    code = "suite_error"


class CheckExecutionError(NexAssureError):
    """A check raised while executing against the warehouse."""

    code = "check_execution_error"


class ExpectationError(NexAssureError):
    """An expectation could not be evaluated (bad operator, shape mismatch)."""

    code = "expectation_error"


class MetastoreError(NexAssureError):
    code = "metastore_error"


class UnsafeSQLError(NexAssureError):
    """A user-supplied query was rejected by the read-only SQL guard."""

    code = "unsafe_sql"
