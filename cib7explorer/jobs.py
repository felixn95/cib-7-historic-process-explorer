"""Background work with progress reporting.

Anything that takes noticeably longer than a page load runs in the background with a progress
indicator instead of blocking the interface. Building the variable catalogue over a large
history takes tens of seconds -- too long for a request, too short to deserve the persistent
state machine that a dump restore needs.

Hence this small runner: one thread per unit of work, progress kept in memory, result written
to the cache. It deliberately does not survive a server restart. The cache does, and the cache
is the actual precomputation; a half-finished job is worth nothing after a restart anyway.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from .config import redact

log = logging.getLogger("cib7explorer.jobs")


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    profile_name: str
    phase: str = "pending"            # pending | running | done | failed
    message: str = ""
    percent: int | None = None
    step: int = 0
    steps_total: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    result_summary: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.phase in ("done", "failed")

    @property
    def duration_ms(self) -> int | None:
        if not self.started_at:
            return None
        end = self.finished_at or datetime.now(timezone.utc)
        return int((end - self.started_at).total_seconds() * 1000)


class Progress:
    """Handed to the work function so it can report intermediate states."""

    def __init__(self, registry: "JobRegistry", job_id: str, steps_total: int | None) -> None:
        self._registry = registry
        self._job_id = job_id
        self._steps_total = steps_total
        self._step = 0

    def step(self, message: str) -> None:
        self._step += 1
        percent = None
        if self._steps_total:
            percent = min(99, int(100 * self._step / self._steps_total))
        self._registry._update(self._job_id, message=message, step=self._step,
                               steps_total=self._steps_total, percent=percent)

    def note(self, message: str) -> None:
        self._registry._update(self._job_id, message=message)


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()

    # -- reading ---------------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self, kind: str, profile_name: str) -> Job | None:
        with self._lock:
            candidates = [j for j in self._jobs.values()
                          if j.kind == kind and j.profile_name == profile_name]
        if not candidates:
            return None
        return max(candidates, key=lambda j: j.started_at or datetime.min.replace(tzinfo=timezone.utc))

    def running(self, kind: str, profile_name: str) -> Job | None:
        j = self.latest(kind, profile_name)
        return j if j and not j.is_terminal else None

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(),
                          key=lambda j: j.started_at or datetime.min.replace(tzinfo=timezone.utc),
                          reverse=True)

    # -- writing ---------------------------------------------------------------------------

    def _update(self, job_id: str, **kw: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                self._jobs[job_id] = replace(job, **kw)

    def start(
        self,
        kind: str,
        profile_name: str,
        fn: Callable[[Progress], str],
        *,
        steps_total: int | None = None,
        allow_parallel: bool = False,
    ) -> Job:
        """Run ``fn`` in a thread.

        ``fn`` receives a ``Progress`` object and returns a short summary that the interface
        shows next to the "as of" timestamp. Without ``allow_parallel`` a second request for
        work of the same kind on the same profile joins the running job rather than starting a
        competing scan of the same tables.
        """
        if not allow_parallel:
            existing = self.running(kind, profile_name)
            if existing:
                return existing

        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, kind=kind, profile_name=profile_name, phase="running",
                  message="started", steps_total=steps_total, step=0, percent=0,
                  started_at=datetime.now(timezone.utc))
        with self._lock:
            self._jobs[job_id] = job
            # Keep only the last 50 jobs so memory does not grow without bound.
            if len(self._jobs) > 50:
                for old in sorted(self._jobs.values(),
                                  key=lambda j: j.started_at or datetime.min.replace(tzinfo=timezone.utc))[:10]:
                    if old.is_terminal:
                        self._jobs.pop(old.id, None)

        def runner() -> None:
            started = time.perf_counter()
            try:
                summary = fn(Progress(self, job_id, steps_total))
                self._update(job_id, phase="done", percent=100,
                             message="finished", result_summary=summary or "",
                             finished_at=datetime.now(timezone.utc))
                log.info("job %s (%s) finished in %d ms", kind, job_id,
                         int((time.perf_counter() - started) * 1000))
            except Exception as exc:  # noqa: BLE001 -- any failure has to reach the page
                self._update(job_id, phase="failed", message="failed",
                             error=redact(str(exc) or exc.__class__.__name__),
                             finished_at=datetime.now(timezone.utc))
                log.warning("job %s (%s) failed: %s", kind, job_id, redact(str(exc)))

        threading.Thread(target=runner, name=f"cib7-job-{kind}-{job_id}", daemon=True).start()
        return job


#: One registry per process. The web interface uses this instance.
registry = JobRegistry()
