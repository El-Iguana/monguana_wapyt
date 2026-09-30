"""
Plain HTTP routes for the bulk byte movers: export, dump and restore.

Not BFF calls, for the same reason as IguanaXterm's file transfers: the BFF is
JSON, so a dump would travel as base64 — a third larger and resident in
Pyodide's heap — and pytincture caps a BFF body at 2 MiB anyway. These routes
read pytincture's own session cookie, so they share the app's login; the one
state-changing route (restore) also checks the CSRF token the way pytincture's
BFF handler does.

Dump layout is mongodump's: ``<db>/<collection>.bson`` (concatenated BSON)
plus ``<db>/<collection>.metadata.json`` (indexes and options, canonical
Extended JSON), so a dump restores with ``mongorestore`` too, and a zipped
``mongodump`` directory restores here.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import os
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Iterator, Optional

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.auth import current_user_id

# Namespaced so no route can ever collide with pytincture's /{application}/... ones.
router = APIRouter(prefix="/mg")

EXPORT_DEFAULT_LIMIT = 10_000
EXPORT_MAX_LIMIT = 1_000_000
CSV_MAX_ROWS = 100_000
_CHUNK = 256 * 1024
_BATCH = 1000

SYSTEM_DATABASES = ("admin", "local", "config")

RESTORE_MODES = ("skip", "drop", "merge")


def max_restore_bytes() -> int:
    return int(os.getenv("MONGUANA_MAX_RESTORE_BYTES", str(1024**3)))


# ---------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------

def _require_user(request: Request) -> int:
    session = getattr(request, "session", None)
    user = session.get("user") if session else None
    user_id = current_user_id(user if isinstance(user, dict) else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _require_csrf(request: Request) -> None:
    """
    Mirror pytincture's CSRF check. Its BFF handler validates the token; these
    plain routes never pass through it.
    """
    session = getattr(request, "session", None)
    expected = (session or {}).get("csrf_token", "")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not hmac.compare_digest(str(expected), supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def _open(user_id: int, conn_id: int):
    """The pooled client and its backend (for its capabilities)."""
    from services.backends import BackendUnavailable
    from services.mongo_pool import ProfileNotFound, pool

    try:
        return pool.open(user_id, conn_id)
    except ProfileNotFound:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    except BackendUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _client(user_id: int, conn_id: int):
    return _open(user_id, conn_id)[0]


def _safe_filename(*parts: str) -> str:
    name = "_".join(part for part in parts if part)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "export"


def _attachment(filename: str) -> dict:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


# ---------------------------------------------------------------------------
# Export: the current query as JSON or CSV
# ---------------------------------------------------------------------------

@router.get("/export/{conn_id}")
async def export(
    request: Request,
    conn_id: int,
    db: str,
    coll: str,
    format: str = "json",
    filter: str = "",
    sort: str = "",
    projection: str = "",
    limit: int = EXPORT_DEFAULT_LIMIT,
):
    """
    Every document the query matches, up to ``limit`` — not just the page on
    screen. JSON is an array of relaxed Extended JSON documents, one per line,
    so it re-imports with its types (Insert accepts an array).
    """
    from services import mql

    user_id = _require_user(request)
    try:
        query = mql.parse_object(filter, "filter")
        mql.check_query(query)
        order = mql.parse_object(sort, "sort")
        fields = mql.parse_object(projection, "projection") or None
    except mql.MQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be json or csv")
    cap = CSV_MAX_ROWS if format == "csv" else EXPORT_MAX_LIMIT
    count = max(1, min(int(limit or EXPORT_DEFAULT_LIMIT), cap))

    collection = _client(user_id, conn_id)[db][coll]

    def cursor():
        found = collection.find(query, fields)
        if order:
            found = found.sort(list(order.items()))
        return found.limit(count)

    filename = _safe_filename(db, coll)
    if format == "json":
        def lines() -> Iterator[bytes]:
            yield b"[\n"
            first = True
            for doc in cursor():
                text = json.dumps(mql.to_display(doc), ensure_ascii=False)
                yield (("" if first else ",\n") + text).encode()
                first = False
            yield b"\n]\n"

        # A sync iterator: Starlette runs it in its thread pool, so the
        # blocking cursor never touches the event loop.
        return StreamingResponse(
            lines(), media_type="application/json",
            headers=_attachment(f"{filename}.json"),
        )

    def build_csv() -> bytes:
        from services import docfmt

        docs = [mql.to_display(doc) for doc in cursor()]
        columns = docfmt.union_columns(docs, limit=500)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        for doc in docs:
            writer.writerow([
                "" if key not in doc else docfmt.cell_text(doc[key], limit=1_000_000)
                for key in columns
            ])
        # A BOM so Excel reads UTF-8 instead of guessing a code page.
        return ("﻿" + buffer.getvalue()).encode()

    try:
        body = await anyio.to_thread.run_sync(build_csv)
    except Exception as exc:  # noqa: BLE001
        from services.mongo_pool import error_text

        raise HTTPException(status_code=400, detail=error_text(exc)) from None
    return StreamingResponse(
        iter([body]), media_type="text/csv; charset=utf-8",
        headers=_attachment(f"{filename}.csv"),
    )


# ---------------------------------------------------------------------------
# Dump (a job: JobService.start_dump; the ZIP is fetched from here when done)
# ---------------------------------------------------------------------------

def _all_capabilities() -> frozenset:
    """MongoDB's: what callers that name no backend get (and the tests)."""
    from services.backends import CAPABILITIES

    return frozenset(CAPABILITIES)


def decoded_codec():
    """
    How documents are decoded where raw BSON is not used: dates aware and UTC,
    UUIDs standard — as on every MongoClient here (see ``backends.mongodb``).
    """
    import datetime as _dt

    from bson.binary import UuidRepresentation
    from bson.codec_options import CodecOptions

    return CodecOptions(tz_aware=True, tzinfo=_dt.timezone.utc,
                        uuid_representation=UuidRepresentation.STANDARD)


def dump_specs(database, coll: str, capabilities=None) -> list:
    """
    The collections a dump or copy covers: one, or every ordinary one in the
    db. Without ``collection_types`` (tinymongo) there are only names, so
    every one counts as an ordinary collection with no options.
    """
    capabilities = _all_capabilities() if capabilities is None else capabilities
    if "collection_types" in capabilities:
        if coll:
            specs = list(database.list_collections(filter={"name": coll}))
            if not specs:
                raise LookupError(f"Collection {coll!r} not found")
            return specs
        return [
            spec for spec in database.list_collections()
            if spec.get("type", "collection") == "collection"
            and not spec["name"].startswith("system.")
        ]
    names = database.list_collection_names()
    if coll:
        if coll not in names:
            raise LookupError(f"Collection {coll!r} not found")
        return [{"name": coll}]
    return [{"name": name} for name in sorted(names) if not name.startswith("system.")]


def _estimate(collection) -> Optional[int]:
    try:
        return collection.estimated_document_count()
    except Exception:  # noqa: BLE001 - a view or no privilege: no total
        return None


def _raw_documents(database, name: str, capabilities) -> Iterator[bytes]:
    """
    Every document of a collection as BSON bytes. With ``raw_bson`` they are
    never decoded (``RawBSONDocument``); otherwise each is encoded here.
    """
    import bson
    from bson.codec_options import CodecOptions
    from bson.raw_bson import RawBSONDocument

    if "raw_bson" in capabilities:
        raw = CodecOptions(document_class=RawBSONDocument)
        for doc in database.get_collection(name, codec_options=raw).find():
            yield doc.raw
        return
    codec = decoded_codec()
    for doc in database[name].find():
        yield bson.encode(doc, codec_options=codec)


def write_dump(client, db: str, coll: str, path, progress=None, capabilities=None) -> dict:
    """
    Write a mongodump-layout ZIP to ``path``, reporting per collection.

    Documents are streamed to the ZIP, never all held in memory. The original
    held every document of every collection in a list, then the whole ZIP in
    a BytesIO. ``capabilities`` are the source backend's (phase 39): without
    ``raw_bson`` documents are encoded here.
    """
    from bson import json_util

    from services.jobs import Progress

    capabilities = _all_capabilities() if capabilities is None else capabilities
    progress = progress or Progress()
    database = client[db]
    specs = dump_specs(database, coll, capabilities)
    progress.log(f"Dumping {len(specs)} collection(s) from {db}")
    summary: list = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for spec in specs:
            name = spec["name"]
            key = f"{db}.{name}"
            progress.item(key, key, _estimate(database[name]), "documents")
            count = 0
            with archive.open(f"{db}/{name}.bson", "w", force_zip64=True) as out:
                for raw in _raw_documents(database, name, capabilities):
                    out.write(raw)
                    count += 1
                    if count % 500 == 0:
                        progress.advance(key, count)
                        progress.check()
            indexes = list(database[name].list_indexes())
            metadata = {
                "collectionName": name,
                "type": spec.get("type", "collection"),
                "options": spec.get("options", {}),
                "indexes": [_index_metadata(index) for index in indexes],
            }
            archive.writestr(
                f"{db}/{name}.metadata.json",
                json_util.dumps(metadata, json_options=json_util.CANONICAL_JSON_OPTIONS),
            )
            # The estimate was metadata; the count is what was written.
            progress.finish(key, note=f"{count:,} documents, {len(indexes)} index(es)", total=count)
            summary.append({"collection": name, "documents": count, "indexes": len(indexes)})
            progress.log(f"✓ {key}: {count:,} documents, {len(indexes)} index(es)")
            progress.partial({"collections": summary})
    size = Path(path).stat().st_size
    progress.log(f"Done: {size:,} bytes")
    return {"collections": summary, "bytes": size}


def _index_metadata(index) -> dict:
    """An index as mongodump writes it. tinymongo gives ``key`` as a list of pairs."""
    info = dict(index)
    if isinstance(info.get("key"), list):
        from bson.son import SON

        info["key"] = SON(info["key"])
    return info


@router.get("/jobs/{job_id}/download")
async def job_download(request: Request, job_id: str):
    """The file a finished job produced (a dump), for its owner only."""
    from fastapi.responses import FileResponse

    from services import jobs

    user_id = _require_user(request)
    job = jobs.get(job_id, user_id)
    if job is None or job.state != jobs.DONE or job.path is None or not job.path.exists():
        raise HTTPException(status_code=404, detail="No such download (it may have expired)")
    return FileResponse(job.path, media_type="application/zip", filename=job.filename)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

@router.post("/restore/{conn_id}")
async def restore(request: Request, conn_id: int, mode: str = "skip", db: str = ""):
    """
    Upload a dump ZIP and start restoring it as a job; returns the job id.

    The body is the ZIP itself (``application/zip``), not a multipart form:
    nothing needs parsing, and nothing is read until the caller is
    authenticated and the CSRF token checks out. It goes to a file, since the
    job outlives this request.

    ``db`` restores every collection into that one database; without it, each
    goes to the database named by its folder. Modes: ``skip`` keeps documents
    whose ``_id`` exists, ``drop`` empties each collection first, ``merge``
    replaces by ``_id``.
    """
    from services import jobs

    user_id = _require_user(request)
    _require_csrf(request)
    if mode not in RESTORE_MODES:
        raise HTTPException(status_code=400, detail="mode must be skip, drop or merge")
    target_db = (db or "").strip()
    if target_db in SYSTEM_DATABASES:
        raise HTTPException(status_code=400, detail=f"{target_db} is a system database")
    client, backend = _open(user_id, conn_id)
    capabilities = frozenset(backend.capabilities)

    limit = max_restore_bytes()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"Larger than the {limit:,}-byte limit")

    upload = jobs.new_file(".zip")
    try:
        received = 0
        with open(upload, "wb") as out:
            async for block in request.stream():
                received += len(block)
                if received > limit:
                    raise HTTPException(status_code=413, detail=f"Larger than the {limit:,}-byte limit")
                await anyio.to_thread.run_sync(out.write, block)
        if not zipfile.is_zipfile(upload):
            raise HTTPException(status_code=400, detail="That is not a ZIP file")
    except BaseException:
        upload.unlink(missing_ok=True)
        raise

    def work(progress) -> dict:
        try:
            with open(upload, "rb") as archive:
                return restore_archive(client, archive, mode, target_db or None, progress,
                                       capabilities)
        finally:
            upload.unlink(missing_ok=True)

    title = f"Restore into {target_db}" if target_db else "Restore"
    try:
        job = jobs.start(user_id, "restore", title, work)
    except jobs.JobLimit as exc:
        upload.unlink(missing_ok=True)
        raise HTTPException(status_code=429, detail=str(exc)) from None
    return JSONResponse({"ok": True, "job": job.id, "bytes": received})


class _Counting:
    """A read-only stream that remembers how much has been read from it."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.count += len(data)
        return data


def plan_restore(names: list[str], target_db: Optional[str]) -> tuple[list[dict], list[str]]:
    """
    Which ``.bson`` members go where. Pure, so it is unit-tested.

    Returns the jobs and the members skipped with the reason.
    """
    jobs: list[dict] = []
    skipped: list[str] = []
    for name in names:
        if not name.endswith(".bson") or name.endswith("/"):
            continue
        normalized = posixpath.normpath(name).lstrip("/")
        parts = [part for part in normalized.split("/") if part not in ("", ".")]
        if ".." in parts:
            skipped.append(f"{name}: unsafe path")
            continue
        collection = parts[-1][: -len(".bson")]
        folder = parts[-2] if len(parts) >= 2 else ""
        database = target_db or folder
        if not database:
            skipped.append(f"{name}: no database folder; choose a target database")
            continue
        if database in SYSTEM_DATABASES:
            skipped.append(f"{name}: {database} is a system database")
            continue
        if not collection or collection.startswith("system."):
            skipped.append(f"{name}: system collection")
            continue
        base = name[: -len(".bson")]
        jobs.append({
            "member": name,
            "metadata": base + ".metadata.json",
            "db": database,
            "collection": collection,
        })
    return jobs, skipped


class _Writer:
    """
    Writes batches into one collection for restore and copy, in a mode:
    ``skip`` keeps documents whose ``_id`` exists, ``drop`` empties the
    collection first, ``merge`` replaces by ``_id``. ``capabilities`` are the
    target backend's: without ``raw_bson`` it takes decoded documents, and
    without ``bulk_write`` Merge replaces one document at a time.
    """

    def __init__(self, database, name: str, mode: str, capabilities, raw: bool) -> None:
        from bson.codec_options import CodecOptions
        from bson.raw_bson import RawBSONDocument

        self.capabilities = capabilities
        self.mode = mode
        self.outcome = {
            "db": database.name, "collection": name,
            "inserted": 0, "replaced": 0, "skipped": 0, "errors": [], "indexes": 0,
        }
        self.collection = (
            database.get_collection(name, codec_options=CodecOptions(document_class=RawBSONDocument))
            if raw else database[name]
        )
        if mode == "drop":
            self.collection.drop()

    @property
    def documents(self) -> int:
        return self.outcome["inserted"] + self.outcome["replaced"] + self.outcome["skipped"]

    def _error(self, text: str) -> None:
        if len(self.outcome["errors"]) < 5:
            self.outcome["errors"].append(text)

    def flush(self, batch: list) -> None:
        from pymongo import ReplaceOne
        from pymongo.errors import BulkWriteError

        if not batch:
            return
        if self.mode == "merge":
            if "bulk_write" in self.capabilities:
                result = self.collection.bulk_write(
                    [ReplaceOne({"_id": doc["_id"]}, doc, upsert=True) for doc in batch],
                    ordered=False,
                )
                self.outcome["inserted"] += result.upserted_count
                self.outcome["replaced"] += result.matched_count
                return
            for doc in batch:
                try:
                    result = self.collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                except Exception as exc:  # noqa: BLE001 - one bad document, not the batch
                    self._error(str(exc)[:300])
                    continue
                if result.upserted_id is not None:
                    self.outcome["inserted"] += 1
                else:
                    self.outcome["replaced"] += result.matched_count
            return
        try:
            self.collection.insert_many(batch, ordered=False)
            # Not len(result.inserted_ids): pymongo leaves that empty for
            # RawBSONDocument inserts.
            self.outcome["inserted"] += len(batch)
        except BulkWriteError as exc:
            details = exc.details or {}
            self.outcome["inserted"] += int(details.get("nInserted") or 0)
            for error in details.get("writeErrors", []):
                if error.get("code") == 11000 and self.mode == "skip":
                    self.outcome["skipped"] += 1
                else:
                    self._error(error.get("errmsg", "write error"))

    def indexes(self, specs: list) -> None:
        """Each index on its own: one the target refuses does not stop the rest."""
        for index in specs:
            if index.get("name") == "_id_":
                continue
            options = {
                key: value for key, value in index.items()
                if key not in ("key", "v", "ns", "background")
            }
            try:
                self.collection.create_index(list(dict(index["key"]).items()), **options)
                self.outcome["indexes"] += 1
            except Exception as exc:  # noqa: BLE001 - reported, the data is in
                self._error(f"index {index.get('name', '?')}: {str(exc)[:200]}")

    def note(self) -> str:
        outcome = self.outcome
        parts = [f"{outcome['inserted']:,} inserted"]
        if outcome["replaced"]:
            parts.append(f"{outcome['replaced']:,} replaced")
        if outcome["skipped"]:
            parts.append(f"{outcome['skipped']:,} skipped")
        if outcome["indexes"]:
            parts.append(f"{outcome['indexes']} index(es)")
        return ", ".join(parts)


def _finish(progress, key: str, writer: _Writer, results: list, extra: dict) -> None:
    outcome = writer.outcome
    results.append(outcome)
    note = writer.note()
    progress.finish(key, "failed" if outcome["errors"] and not outcome["inserted"] else "done", note)
    progress.log(f"{'⚠' if outcome['errors'] else '✓'} {key}: {note}")
    for error in outcome["errors"]:
        progress.log(f"    {error}")
    progress.partial({"collections": results, **extra})


def restore_archive(client, archive_file, mode: str, target_db: Optional[str],
                    progress=None, capabilities=None) -> dict:
    """
    Restore every planned collection, reporting bytes read per collection
    (a member's uncompressed size is its total) and stopping between batches
    if cancelled. What was restored before a cancel is kept as the result.
    ``capabilities`` are the target backend's (phase 39).
    """
    import bson
    from bson import json_util
    from bson.codec_options import CodecOptions
    from bson.raw_bson import RawBSONDocument

    from services.jobs import Cancelled, Progress

    capabilities = _all_capabilities() if capabilities is None else capabilities
    progress = progress or Progress()
    raw = "raw_bson" in capabilities
    codec = CodecOptions(document_class=RawBSONDocument) if raw else decoded_codec()
    results: list[dict] = []
    with zipfile.ZipFile(archive_file) as archive:
        members = set(archive.namelist())
        jobs, skipped = plan_restore(sorted(members), target_db)
        for entry in skipped:
            progress.log(f"– skipped {entry}")
        for job in jobs:
            key = f"{job['db']}.{job['collection']}"
            progress.item(key, key, archive.getinfo(job["member"]).file_size, "bytes")
        progress.log(f"Restoring {len(jobs)} collection(s), mode {mode}")
        for job in jobs:
            key = f"{job['db']}.{job['collection']}"
            writer = _Writer(client[job["db"]], job["collection"], mode, capabilities, raw)
            try:
                with archive.open(job["member"]) as member:
                    stream = _Counting(member)
                    batch: list = []
                    for doc in bson.decode_file_iter(stream, codec_options=codec):
                        batch.append(doc)
                        if len(batch) >= _BATCH:
                            writer.flush(batch)
                            batch = []
                            progress.advance(key, stream.count, note=f"{writer.documents:,} documents")
                            progress.check()
                    writer.flush(batch)
                    progress.advance(key, stream.count, note=f"{writer.documents:,} documents")
            except Cancelled:
                results.append(writer.outcome)
                progress.partial({"collections": results, "skipped": skipped})
                raise
            except Exception as exc:  # noqa: BLE001 - reported per collection
                writer._error(str(exc)[:300])

            if job["metadata"] in members:
                try:
                    metadata = json_util.loads(archive.read(job["metadata"]))
                    writer.indexes(metadata.get("indexes", []))
                except Exception as exc:  # noqa: BLE001
                    writer._error(f"indexes: {str(exc)[:300]}")
            _finish(progress, key, writer, results, {"skipped": skipped})
    return {"collections": results, "skipped": skipped}


def copy_collections(source, target, db: str, coll: str, target_db: str, target_coll: str,
                     mode: str, progress=None, source_capabilities=None,
                     target_capabilities=None) -> dict:
    """
    Copy a database (``coll`` empty) or one collection from one connection to
    another, any backends (phase 39, step 5): the documents in batches, then
    the indexes. Raw BSON end to end when both sides have it, decoded
    otherwise. ``target_coll`` renames a single collection on the way.
    """
    from bson.codec_options import CodecOptions
    from bson.raw_bson import RawBSONDocument

    from services.jobs import Cancelled, Progress

    source_capabilities = (_all_capabilities() if source_capabilities is None
                           else source_capabilities)
    target_capabilities = (_all_capabilities() if target_capabilities is None
                           else target_capabilities)
    progress = progress or Progress()
    raw = "raw_bson" in source_capabilities and "raw_bson" in target_capabilities
    codec = CodecOptions(document_class=RawBSONDocument) if raw else decoded_codec()
    specs = dump_specs(source[db], coll, source_capabilities)
    plan = [(spec["name"], (target_coll or spec["name"]) if coll else spec["name"]) for spec in specs]
    for name, new_name in plan:
        progress.item(f"{db}.{name}", f"{db}.{name} → {target_db}.{new_name}",
                      _estimate(source[db][name]), "documents")
    progress.log(f"Copying {len(plan)} collection(s) to {target_db}, mode {mode}")
    results: list[dict] = []
    for name, new_name in plan:
        key = f"{db}.{name}"
        writer = _Writer(target[target_db], new_name, mode, target_capabilities, raw)
        read = 0
        try:
            batch: list = []
            for doc in source[db].get_collection(name, codec_options=codec).find():
                batch.append(doc)
                read += 1
                if len(batch) >= _BATCH:
                    writer.flush(batch)
                    batch = []
                    progress.advance(key, read)
                    progress.check()
            writer.flush(batch)
            progress.advance(key, read)
        except Cancelled:
            results.append(writer.outcome)
            progress.partial({"collections": results})
            raise
        except Exception as exc:  # noqa: BLE001 - reported per collection
            writer._error(str(exc)[:300])
        try:
            writer.indexes([_index_metadata(index) for index in source[db][name].list_indexes()])
        except Exception as exc:  # noqa: BLE001
            writer._error(f"indexes: {str(exc)[:300]}")
        _finish(progress, key, writer, results, {})
    return {"collections": results}
