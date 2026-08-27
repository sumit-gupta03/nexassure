# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Cron scheduling.

Suites carry an optional ``schedule:`` cron expression. ``nexassure schedule run``
starts a long-lived process that fires them at the right times - a single
container that keeps a warehouse continuously tested, with no Airflow required.

Built on ``croniter`` (already a core dependency) rather than a heavier
scheduler, because the requirements here are narrow and the failure modes of a
data quality scheduler need to be obvious:

* **No overlap.** A suite still running when its next slot arrives is skipped,
  not queued. Two concurrent runs of the same suite would double warehouse cost
  and produce interleaved history for no benefit.
* **No catch-up.** After downtime, the next fire is the next future slot. A
  scheduler that replays six hours of missed data quality runs at once creates
  an alert storm about the same underlying problem.
* **Failures do not stop the loop.** A suite that raises is logged and
  rescheduled.

For teams that already run Airflow, Dagster or Kubernetes CronJobs, call
``nexassure run`` from there instead and skip this module entirely.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from croniter import CroniterBadCronError, croniter

from ..core.models import RunResult, Suite, utcnow
from ..exceptions import ConfigError
from ..logging_conf import get_logger

log = get_logger(__name__)

#: How often the loop wakes to check for due jobs.
TICK_SECONDS = 5.0


def validate_cron(expression: str) -> None:
    """Raise :class:`ConfigError` if ``expression`` is not a valid cron string."""
    try:
        croniter(expression)
    except (CroniterBadCronError, ValueError, KeyError) as exc:
        raise ConfigError(
            f"Invalid cron expression {expression!r}: {exc}. "
            "Expected 5 fields (m h dom mon dow), or 6 with seconds.",
            cron=expression,
        ) from exc


def next_fire_time(expression: str, after: datetime | None = None) -> datetime:
    """The next time ``expression`` fires, strictly after ``after``."""
    validate_cron(expression)
    base = after or utcnow()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return croniter(expression, base).get_next(datetime)


def describe_schedule(expression: str, count: int = 3) -> list[datetime]:
    """Preview the next few fire times - used by ``nexassure schedule list``."""
    validate_cron(expression)
    iterator = croniter(expression, utcnow())
    return [iterator.get_next(datetime) for _ in range(count)]


@dataclass
class ScheduledJob:
    """One suite bound to a cron expression."""

    name: str
    suite_name: str
    cron: str
    enabled: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_run_id: str | None = None
    run_count: int = 0
    failure_count: int = 0
    #: Guards against overlapping executions of this job.
    _running: bool = field(default=False, repr=False)

    def schedule_next(self, after: datetime | None = None) -> datetime:
        self.next_run_at = next_fire_time(self.cron, after)
        return self.next_run_at

    def is_due(self, now: datetime) -> bool:
        return (
            self.enabled
            and not self._running
            and self.next_run_at is not None
            and now >= self.next_run_at
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "suite": self.suite_name,
            "cron": self.cron,
            "enabled": self.enabled,
            "running": self._running,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_status": self.last_status,
            "last_run_id": self.last_run_id,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
        }


class Scheduler:
    """Runs scheduled suites in a background thread pool.

    Args:
        run_suite: Callable invoked as ``run_suite(suite_name)``; normally
            ``NexAssure.run_suite``. Injected rather than imported so the scheduler
            stays testable without a warehouse.
        metastore: Optional store for recording fire times and outcomes.
        max_workers: Ceiling on concurrently executing suites.
    """

    def __init__(
        self,
        run_suite: Callable[[str], RunResult],
        metastore: Any = None,
        max_workers: int = 4,
    ) -> None:
        self._run_suite = run_suite
        self._metastore = metastore
        self._max_workers = max_workers
        self._jobs: dict[str, ScheduledJob] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._on_complete: Callable[[str, RunResult], None] | None = None

    # -- registration ------------------------------------------------------- #

    def add(self, name: str, suite_name: str, cron: str, *, enabled: bool = True) -> ScheduledJob:
        """Register a job. Re-registering the same name replaces it."""
        validate_cron(cron)
        job = ScheduledJob(name=name, suite_name=suite_name, cron=cron, enabled=enabled)
        job.schedule_next()
        with self._lock:
            self._jobs[name] = job
        if self._metastore is not None:
            self._metastore.upsert_schedule(
                name, suite_name, cron, enabled=enabled, next_run_at=job.next_run_at
            )
        log.info("Scheduled %r (%s) - next run %s", name, cron, job.next_run_at)
        return job

    def add_suites(self, suites: list[Suite]) -> list[ScheduledJob]:
        """Register every suite that declares a ``schedule:``."""
        registered = []
        for suite in suites:
            if not suite.schedule:
                continue
            try:
                registered.append(self.add(suite.name, suite.name, suite.schedule))
            except ConfigError as exc:
                log.error("Skipping suite %r: %s", suite.name, exc)
        return registered

    def remove(self, name: str) -> bool:
        with self._lock:
            removed = self._jobs.pop(name, None) is not None
        if removed and self._metastore is not None:
            self._metastore.delete_schedule(name)
        return removed

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.to_dict() for job in sorted(self._jobs.values(), key=lambda j: j.name)]

    def on_complete(self, callback: Callable[[str, RunResult], None]) -> None:
        """Register a callback fired after each scheduled run finishes."""
        self._on_complete = callback

    # -- execution ---------------------------------------------------------- #

    def start(self, block: bool = True, install_signal_handlers: bool = True) -> None:
        """Start the scheduling loop.

        Args:
            block: Run in the foreground until stopped. When ``False`` the loop
                runs on a daemon thread and control returns immediately.
            install_signal_handlers: Handle SIGINT/SIGTERM for a clean shutdown.
                Only valid on the main thread, so this is skipped automatically
                when the scheduler is embedded elsewhere.
        """
        self._stop.clear()
        if install_signal_handlers and threading.current_thread() is threading.main_thread():
            self._install_signals()

        if not self._jobs:
            log.warning("Scheduler started with no jobs registered")

        if block:
            self._loop()
        else:
            thread = threading.Thread(target=self._loop, name="nexassure-scheduler", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 30.0) -> None:
        """Signal shutdown and wait for in-flight runs to finish."""
        log.info("Scheduler stopping")
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in list(self._threads):
            remaining = max(deadline - time.monotonic(), 0.0)
            thread.join(timeout=remaining)
        self._threads = [t for t in self._threads if t.is_alive()]
        if self._threads:
            log.warning("%s scheduler thread(s) still running at shutdown", len(self._threads))

    def _install_signals(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            log.info("Received signal %s", signum)
            self._stop.set()

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
            # Signal registration fails off the main thread and on some
            # platforms; the scheduler still runs, just without clean shutdown.
            with suppress(ValueError, OSError):  # pragma: no cover - platform dependent
                signal.signal(sig, handler)

    def _loop(self) -> None:
        log.info("Scheduler running with %s job(s)", len(self._jobs))
        while not self._stop.is_set():
            now = utcnow()
            for job in self._due_jobs(now):
                self._dispatch(job)
            # Event.wait doubles as the sleep and the shutdown signal, so
            # stopping is immediate rather than up to a tick late.
            self._stop.wait(TICK_SECONDS)
        log.info("Scheduler loop exited")

    def _due_jobs(self, now: datetime) -> list[ScheduledJob]:
        with self._lock:
            due = [job for job in self._jobs.values() if job.is_due(now)]
            active = sum(1 for job in self._jobs.values() if job._running)
            capacity = max(self._max_workers - active, 0)
            for job in due[:capacity]:
                job._running = True
            skipped = due[capacity:]
        for job in skipped:
            log.warning(
                "Job %r is due but the worker pool is full (%s running); "
                "it will fire on the next tick",
                job.name,
                self._max_workers,
            )
        return due[:capacity]

    def _dispatch(self, job: ScheduledJob) -> None:
        thread = threading.Thread(
            target=self._execute, args=(job,), name=f"nexassure-job-{job.name}", daemon=True
        )
        thread.start()
        self._threads.append(thread)
        self._threads = [t for t in self._threads if t.is_alive()]

    def _execute(self, job: ScheduledJob) -> None:
        started = utcnow()
        log.info("Firing scheduled job %r (suite %r)", job.name, job.suite_name)
        run: RunResult | None = None
        status = "errored"
        try:
            run = self._run_suite(job.suite_name)
            status = run.status.value
        except Exception as exc:
            log.error("Scheduled job %r failed: %s", job.name, exc, exc_info=True)
        finally:
            with self._lock:
                job._running = False
                job.last_run_at = started
                job.last_status = status
                job.run_count += 1
                if status not in ("passed",):
                    job.failure_count += 1
                if run is not None:
                    job.last_run_id = run.run_id
                # Schedule from now, not from the planned slot: a run that
                # overran its own interval should not fire again immediately.
                job.schedule_next(utcnow())

            if self._metastore is not None:
                try:
                    self._metastore.mark_schedule_run(
                        job.name, run.run_id if run else "", status, job.next_run_at
                    )
                except Exception as exc:
                    log.debug("Could not record schedule outcome: %s", exc)

            if self._on_complete is not None and run is not None:
                try:
                    self._on_complete(job.name, run)
                except Exception as exc:
                    log.debug("Schedule completion callback raised: %s", exc)

            log.info("Job %r finished %s - next run %s", job.name, status, job.next_run_at)

    def run_now(self, name: str) -> RunResult:
        """Fire a registered job immediately, outside its schedule."""
        with self._lock:
            job = self._jobs.get(name)
        if job is None:
            raise ConfigError(f"Unknown scheduled job {name!r}", requested=name)
        return self._run_suite(job.suite_name)
