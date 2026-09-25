"""Background jobs: progress, cancel, scoping, limits and cleanup."""
from __future__ import annotations

import threading
import time

import pytest

from services import jobs


def wait(job, timeout=5.0):
    deadline = time.time() + timeout
    while job.state == jobs.RUNNING and time.time() < deadline:
        time.sleep(0.02)
    return job.snapshot()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from services import db

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(jobs, "_jobs", {})


def test_progress_and_result():
    def work(progress):
        progress.item("a", "db.a", 10, "documents")
        progress.advance("a", 4)
        progress.log("half way")
        progress.finish("a", note="10 documents", total=12)
        return {"n": 12}

    snap = wait(jobs.start(1, "dump", "Dump db", work))
    assert snap["state"] == "done" and snap["result"] == {"n": 12}
    assert snap["items"] == [{"key": "a", "label": "db.a", "total": 12, "done": 12,
                              "unit": "documents", "state": "done", "note": "10 documents"}]
    assert snap["log"] == ["half way"] and snap["log_total"] == 1


def test_log_from_returns_only_new_lines():
    job = wait(jobs.start(1, "x", "x", lambda p: [p.log(f"line {i}") for i in range(3)] and {}))
    job = jobs.get(job["id"], 1)
    assert job.snapshot(log_from=2)["log"] == ["line 2"]


def test_cancel_keeps_the_partial_result_and_removes_the_file():
    started = threading.Event()
    path = jobs.new_file(".zip")
    path.write_bytes(b"partial")

    def work(progress):
        progress.item("a", "a", None)
        progress.partial({"done": 1})
        started.set()
        while True:
            progress.check()
            time.sleep(0.01)

    job = jobs.start(1, "dump", "x", work, path=path, filename="x.zip")
    started.wait(2)
    assert not jobs.cancel(job.id, 2), "another user cannot cancel it"
    assert jobs.cancel(job.id, 1)
    snap = wait(job)
    assert snap["state"] == "cancelled" and snap["result"] == {"done": 1}
    assert snap["items"][0]["state"] == "cancelled"
    assert not path.exists() and snap["download"] is False


def test_failures_are_reported():
    snap = wait(jobs.start(1, "x", "x", lambda p: 1 / 0))
    assert snap["state"] == "failed" and "division" in snap["error"]


def test_jobs_are_per_user():
    job = jobs.start(1, "x", "x", lambda p: {})
    assert jobs.get(job.id, 1) is job and jobs.get(job.id, 2) is None


def test_running_jobs_are_limited_per_user():
    release = threading.Event()
    running = [jobs.start(1, "x", "x", lambda p: release.wait(5) and {})
               for _ in range(jobs.MAX_RUNNING_PER_USER)]
    with pytest.raises(jobs.JobLimit):
        jobs.start(1, "x", "x", lambda p: {})
    jobs.start(2, "x", "x", lambda p: {})  # someone else is not affected
    release.set()
    for job in running:
        wait(job)


def test_finished_jobs_and_files_are_purged():
    path = jobs.new_file(".zip")
    path.write_bytes(b"zip")
    job = jobs.start(1, "dump", "x", lambda p: {}, path=path, filename="x.zip")
    assert wait(job)["download"] is True
    jobs.purge(now=time.time() + jobs.KEEP_FINISHED_SECONDS + 1)
    assert jobs.get(job.id, 1) is None and not path.exists()
