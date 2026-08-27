# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Logging setup.

NexAssure logs to stderr so that machine-readable output (JSON reports, MCP stdio
frames) can own stdout without interleaving.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line — friendly to Loki/Datadog/CloudWatch."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | int = "INFO", json_output: bool | None = None) -> None:
    """Idempotently configure the ``nexassure`` logger tree."""
    global _CONFIGURED
    if json_output is None:
        json_output = os.getenv("NEXASSURE_LOG_FORMAT", "").lower() == "json"

    root = logging.getLogger("nexassure")
    if _CONFIGURED:
        root.setLevel(level)
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger."""
    return logging.getLogger(name if name.startswith("nexassure") else f"nexassure.{name}")
