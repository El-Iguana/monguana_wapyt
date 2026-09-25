"""
BFF: start a dump, and follow or cancel any job (ROADMAP phase 36).

The registry lives in ``services.jobs`` (a plain module); this file is
re-executed per call and keeps nothing. A restore starts from its upload
route in ``transfer.py``, since the ZIP arrives there.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id


@backend_for_frontend
@bff_policy(application="monguana")
class JobService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    def start_dump(self, conn_id: int, db: str, coll: str = "") -> dict:
        """Dump a database, or one collection, to a ZIP; returns the job id."""
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        from services import jobs
        from services.mongo_pool import ProfileNotFound, pool
        from services.transfer import _safe_filename, write_dump

        try:
            client = pool.client(self._user_id, int(conn_id))
        except ProfileNotFound:
            return {"ok": False, "error": "Connection not found"}
        path = jobs.new_file(".zip")
        title = f"Dump {db}.{coll}" if coll else f"Dump {db}"
        try:
            job = jobs.start(
                self._user_id, "dump", title,
                lambda progress: write_dump(client, db, coll, path, progress),
                path=path, filename=f"{_safe_filename(db, coll)}.zip",
            )
        except jobs.JobLimit as exc:
            path.unlink(missing_ok=True)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "job": job.id}

    def status(self, job_id: str, log_from: int = 0) -> dict:
        from services import jobs

        job = jobs.get(job_id, self._user_id)
        if job is None:
            return {"ok": False, "error": "No such job (it may have expired)"}
        return {"ok": True, **job.snapshot(log_from)}

    def cancel(self, job_id: str) -> dict:
        from services import jobs

        if not jobs.cancel(job_id, self._user_id):
            return {"ok": False, "error": "That job is not running"}
        return {"ok": True}
