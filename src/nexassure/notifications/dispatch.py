# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Failure notifications.

Two sinks ship in the box: a Slack incoming webhook and a generic JSON webhook
that fits PagerDuty, Opsgenie, Teams and anything else that accepts a POST.

Delivery is best-effort and time-boxed. A notification sink that is slow or
down must never extend or fail a test run, so every send is wrapped and errors
are logged rather than raised.
"""

from __future__ import annotations

from typing import Any

from ..core.enums import CheckStatus
from ..core.models import RunResult
from ..logging_conf import get_logger

log = get_logger(__name__)

#: Failures listed inline before the message switches to a summary line.
MAX_LISTED_FAILURES = 10
#: Seconds to wait on a webhook before giving up.
SEND_TIMEOUT = 10.0

_STATUS_EMOJI = {
    "passed": ":white_check_mark:",
    "failed": ":x:",
    "errored": ":rotating_light:",
    "cancelled": ":black_square_for_stop:",
}


def build_payload(run: RunResult, include_passed: bool = False) -> dict[str, Any]:
    """Structured summary of a run, for the generic webhook sink."""
    results = run.results if include_passed else run.failures()
    return {
        "event": "nexassure.run.completed",
        "run_id": run.run_id,
        "suite": run.suite_name,
        "connection": run.connection_name,
        "status": run.status.value,
        "environment": run.environment,
        "triggered_by": run.triggered_by,
        "started_at": run.started_at.isoformat(),
        "duration_ms": run.duration_ms,
        "summary": run.summary.model_dump(),
        "pass_rate": run.summary.pass_rate,
        "results": [
            {
                "check": r.check_name,
                "type": r.check_type,
                "status": r.status.value,
                "severity": r.severity.value,
                "dataset": r.dataset,
                "column": r.column,
                "description": r.description,
                "message": r.message,
                "observed": r.observed,
                "expected": r.expected,
                "rows_failed": r.rows_failed,
                "owner": r.owner,
            }
            for r in results
        ],
    }


def build_slack_blocks(run: RunResult) -> dict[str, Any]:
    """Slack Block Kit message.

    ``text`` is set as well as ``blocks`` because Slack uses it for the
    notification preview and for clients that cannot render blocks.
    """
    emoji = _STATUS_EMOJI.get(run.status.value, ":grey_question:")
    s = run.summary
    headline = f"{emoji} NexAssure - {run.suite_name} {run.status.value}"

    fields = [
        f"*Connection*\n{run.connection_name}",
        f"*Checks*\n{s.total} total",
        f"*Passed*\n{s.passed}",
        f"*Failed*\n{s.failed + s.errored}",
    ]
    if run.environment:
        fields.append(f"*Environment*\n{run.environment}")
    fields.append(f"*Duration*\n{run.duration_ms / 1000:.1f}s")

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline[:150]}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields[:10]]},
    ]

    failures = run.failures()
    if failures:
        lines = []
        for result in failures[:MAX_LISTED_FAILURES]:
            target = result.dataset or "-"
            if result.column:
                target = f"{target}.{result.column}"
            icon = ":rotating_light:" if result.status is CheckStatus.ERRORED else ":x:"
            lines.append(f"{icon} *{result.check_name}* ({target})\n    {result.message}")
        if len(failures) > MAX_LISTED_FAILURES:
            lines.append(f"_...and {len(failures) - MAX_LISTED_FAILURES} more_")
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}}
        )

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"run `{run.run_id}` - NexAssure"}],
        }
    )
    return {"text": headline, "blocks": blocks}


def send_webhook(url: str, payload: dict[str, Any]) -> bool:
    """POST JSON to a URL. Returns whether it was accepted."""
    try:
        import httpx
    except ImportError:
        log.warning("Notifications need httpx. Install with: pip install 'nexassure[notify]'")
        return False

    try:
        response = httpx.post(url, json=payload, timeout=SEND_TIMEOUT)
        if response.status_code >= 400:
            log.warning("Webhook returned %s: %s", response.status_code, response.text[:200])
            return False
        return True
    except Exception as exc:
        log.warning("Webhook delivery failed: %s", exc)
        return False


def dispatch(run: RunResult, settings: Any) -> dict[str, bool]:
    """Send a run to every configured sink.

    Args:
        run: The completed run.
        settings: A ``NotificationConfig``-shaped object.

    Returns:
        ``{sink_name: delivered}``.
    """
    delivered: dict[str, bool] = {}

    slack_url = getattr(settings, "slack_webhook", None)
    if slack_url:
        delivered["slack"] = send_webhook(slack_url, build_slack_blocks(run))

    webhook_url = getattr(settings, "webhook_url", None)
    if webhook_url:
        payload = build_payload(run, getattr(settings, "include_passed", False))
        delivered["webhook"] = send_webhook(webhook_url, payload)

    if delivered:
        log.info("Notifications dispatched: %s", delivered)
    return delivered
