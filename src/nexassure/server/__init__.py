# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Optional REST API."""

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    """Build the FastAPI application. Requires ``pip install 'nexassure[server]'``."""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
