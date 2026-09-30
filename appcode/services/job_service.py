"""
BFF: start a dump or a copy, and follow or cancel any job (ROADMAP phases 36
and 39).

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
        from services.backends import BackendUnavailable
        from services.mongo_pool import ProfileNotFound, pool
        from services.transfer import _safe_filename, write_dump

        try:
            client, backend = pool.open(self._user_id, int(conn_id))
        except ProfileNotFound:
            return {"ok": False, "error": "Connection not found"}
        except BackendUnavailable as exc:
            return {"ok": False, "error": str(exc)}
        path = jobs.new_file(".zip")
        title = f"Dump {db}.{coll}" if coll else f"Dump {db}"
        try:
            job = jobs.start(
                self._user_id, "dump", title,
                lambda progress: write_dump(client, db, coll, path, progress,
                                            frozenset(backend.capabilities)),
                path=path, filename=f"{_safe_filename(db, coll)}.zip",
            )
        except jobs.JobLimit as exc:
            path.unlink(missing_ok=True)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "job": job.id}

    def start_copy(
        self, conn_id: int, db: str, target_conn: int, target_db: str,
        coll: str = "", target_coll: str = "", mode: str = "skip",
    ) -> dict:
        """
        Copy a database (no ``coll``) or a collection to any of the caller's
        connections, whatever their backends (phase 39, step 5).
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        from services import jobs
        from services.backends import BackendUnavailable
        from services.mongo_pool import ProfileNotFound, pool
        from services.transfer import RESTORE_MODES, SYSTEM_DATABASES, copy_collections

        db, coll = (db or "").strip(), (coll or "").strip()
        target_db = (target_db or "").strip() or db
        target_coll = (target_coll or "").strip() if coll else ""
        if mode not in RESTORE_MODES:
            return {"ok": False, "error": "mode must be skip, drop or merge"}
        if not db:
            return {"ok": False, "error": "Choose what to copy"}
        if target_db in SYSTEM_DATABASES:
            return {"ok": False, "error": f"{target_db} is a system database"}
        import re

        if re.search(r'[/\\. "$*<>:|?\x00]', target_db) or len(target_db.encode()) > 63:
            return {"ok": False, "error": "That is not a valid database name"}
        if target_coll and (target_coll.startswith("system.") or "$" in target_coll
                            or "\x00" in target_coll):
            return {"ok": False, "error": "That is not a valid collection name"}
        same_place = int(conn_id) == int(target_conn) and db == target_db and (
            not coll or (target_coll or coll) == coll)
        if same_place:
            return {"ok": False, "error": "That copies onto itself; choose another target"}
        try:
            source, source_backend = pool.open(self._user_id, int(conn_id))
            target, target_backend = pool.open(self._user_id, int(target_conn))
        except ProfileNotFound:
            return {"ok": False, "error": "Connection not found"}
        except BackendUnavailable as exc:
            return {"ok": False, "error": str(exc)}
        title = f"Copy {db}.{coll}" if coll else f"Copy {db}"
        try:
            job = jobs.start(
                self._user_id, "copy", title,
                lambda progress: copy_collections(
                    source, target, db, coll, target_db, target_coll, mode, progress,
                    frozenset(source_backend.capabilities), frozenset(target_backend.capabilities)),
            )
        except jobs.JobLimit as exc:
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
