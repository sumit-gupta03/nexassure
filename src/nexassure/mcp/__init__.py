# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Model Context Protocol server exposing NexAssure to AI agents."""

__all__ = ["main"]


def main() -> None:
    """Entry point for the ``nexassure-mcp`` console script."""
    from .server import main as _main

    _main()
