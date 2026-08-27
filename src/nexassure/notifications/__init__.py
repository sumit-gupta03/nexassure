# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Outbound notifications for run outcomes."""

from .dispatch import build_payload, build_slack_blocks, dispatch, send_webhook

__all__ = ["build_payload", "build_slack_blocks", "dispatch", "send_webhook"]
