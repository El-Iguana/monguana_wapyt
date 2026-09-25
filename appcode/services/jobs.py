"""
Background jobs with progress: dump and restore (ROADMAP phase 36).

**Not a BFF module, on purpose** — pytincture re-executes those on every
call, which would give each status poll an empty registry (IguanaXterm's
"No such download" on the first poll). This is imported normally, so
``_jobs`` is a real singleton.

**A job, not a stream.** pytincture caps a ``@bff_stream`` at 300 s total and
30 s between items; a large dump outlasts both. A job runs on its own thread,
the page polls ``JobService.status`` about twice a second, and closing the
page does not stop it. Cancel is cooperative: work calls ``progress.check()``
between batches.

A job's work function receives a :class:`Progress` and returns a JSON-safe
result dict. Files a job produces live in ``DATA_DIR/jobs`` and are removed
with the job.
"""
from __future__ import annotations

import secrets
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from services import db


def jobs_dir() -> Path:
    """Where job files go; read per call so tests can move the data dir."""
    return Path(db.DATA_DIR) / "jobs"

# A finished job (and any file it made) is kept this long for its page to
# collect the result or the download, then dropped.
KEEP_FINISHED_SECONDS = 60 * 60
MAX_RUNNING_PER_USER = 3
MAX_LOG_LINES = 500

RUNNING, DONE, FAILED, CANCELLED = "running", "done", "failed", "cancelled"


class Cancelled(Exception):
    """Raised by ``Progress.check()`` once cancel has been asked for."""


class JobLimit(RuntimeError):
    pass


class Job:
    def __init__(self, user_id: int, kind: str, title: str) -> None:
        self.id = secrets.token_urlsafe(12)
        self.user_id = int(user_id)
        self.kind = kind
        self.title = title
        self.state = RUNNING
        self.started = time.time()
        self.finished: Optional[float] = None
        self.items: dict[str, dict] = {}
        self.log: deque = deque(maxlen=MAX_LOG_LINES)
        self.log_total = 0
        self.result: dict = {}
        self.error = ""
        self.path: Optional[Path] = None     # a file the job produced
        self.filename = ""                   # its download name
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()

    def snapshot(self, log_from: int = 0) -> dict:
        """JSON-safe state. ``log_from`` returns only lines after that many."""
        with self.lock:
            skipped = self.log_total - len(self.log)
            start = max(0, int(log_from or 0) - skipped)
            lines = list(self.log)[start:]
            return {
                "id": self.id,
                "kind": self.kind,
                "title": self.title,
                "state": self.state,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
                "items": [dict(item, key=key) for key, item in self.items.items()],
                "log": lines,
                "log_total": self.log_total,
                "result": self.result,
                "error": self.error,
                "download": bool(self.state == DONE and self.path and self.path.exists()),
                "cancelling": self.cancel_event.is_set() and self.state == RUNNING,
            }


class Progress:
    """What a job's work function reports through."""

    def __init__(self, job: Optional[Job] = None) -> None:
        self._job = job

    def item(self, key: str, label: str, total: Optional[int] = None, unit: str = "") -> None:
        if self._job is None:
            return
        with self._job.lock:
            self._job.items[key] = {
                "label": label, "total": total, "done": 0, "unit": unit,
                "state": RUNNING, "note": "",
            }

    def advance(self, key: str, done: int, note: str = "") -> None:
        if self._job is None:
            return
        with self._job.lock:
            item = self._job.items.get(key)
            if item is not None:
                item["done"] = done
                if note:
                    item["note"] = note

    def finish(self, key: str, state: str = DONE, note: str = "",
               total: Optional[int] = None) -> None:
        """Close an item. ``total`` replaces an estimate with the true figure."""
        if self._job is None:
            return
        with self._job.lock:
            item = self._job.items.get(key)
            if item is not None:
                item["state"] = state
                if total is not None:
                    item["total"] = total
                if item["total"] is not None and state == DONE:
                    item["done"] = item["total"]
                if note:
                    item["note"] = note

    def log(self, line: str) -> None:
        if self._job is None:
            return
        with self._job.lock:
            self._job.log.append(str(line))
            self._job.log_total += 1

    def partial(self, result: dict) -> None:
        """What the job has achieved so far — kept if it is cancelled."""
        if self._job is None:
            return
        with self._job.lock:
            self._job.result = result

    def check(self) -> None:
        if self._job is not None and self._job.cancel_event.is_set():
            raise Cancelled()


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def start(user_id: int, kind: str, title: str, work: Callable[[Progress], dict],
          *, path: Optional[Path] = None, filename: str = "") -> Job:
    """Register a job and run ``work`` on its own thread."""
    purge()
    with _jobs_lock:
        running = sum(1 for job in _jobs.values()
                      if job.user_id == int(user_id) and job.state == RUNNING)
        if running >= MAX_RUNNING_PER_USER:
            raise JobLimit(f"{running} jobs are already running; wait for one to finish")
        job = Job(user_id, kind, title)
        job.path, job.filename = path, filename
        _jobs[job.id] = job

    def _run() -> None:
        progress = Progress(job)
        try:
            result = work(progress)
        except Cancelled:
            state, error, result = CANCELLED, "", None
            progress.log("Cancelled.")
        except Exception as exc:  # noqa: BLE001 - reported to the page
            from services.mongo_pool import error_text

            state, error, result = FAILED, error_text(exc), None
            progress.log(f"Failed: {error}")
        else:
            state, error = DONE, ""
        with job.lock:
            if result is not None:
                job.result = result
            job.state, job.error, job.finished = state, error, time.time()
            for item in job.items.values():
                if item["state"] == RUNNING:
                    item["state"] = state if state != DONE else DONE
        if state != DONE and job.path is not None:
            _remove(job.path)

    threading.Thread(target=_run, name=f"job-{kind}-{job.id[:6]}", daemon=True).start()
    return job


def get(job_id: str, user_id: int) -> Optional[Job]:
    """A job the caller owns, or None. There is no unscoped lookup."""
    with _jobs_lock:
        job = _jobs.get(str(job_id))
    return job if job is not None and job.user_id == int(user_id) else None


def cancel(job_id: str, user_id: int) -> bool:
    job = get(job_id, user_id)
    if job is None or job.state != RUNNING:
        return False
    job.cancel_event.set()
    return True


def new_file(suffix: str) -> Path:
    folder = jobs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{secrets.token_urlsafe(12)}{suffix}"


def purge(now: Optional[float] = None) -> None:
    """Drop finished jobs past their keep time, with their files."""
    now = now or time.time()
    with _jobs_lock:
        stale = [job_id for job_id, job in _jobs.items()
                 if job.finished and now - job.finished > KEEP_FINISHED_SECONDS]
        dropped = [_jobs.pop(job_id) for job_id in stale]
    for job in dropped:
        if job.path is not None:
            _remove(job.path)


def clear_leftovers() -> None:
    """Files from before a restart belong to jobs that no longer exist."""
    if jobs_dir().exists():
        shutil.rmtree(jobs_dir(), ignore_errors=True)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
