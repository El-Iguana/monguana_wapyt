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
import tempfile
import zipfile
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


def _client(user_id: int, conn_id: int):
    from services.mongo_pool import ProfileNotFound, pool

    try:
        return pool.client(user_id, conn_id)
    except ProfileNotFound:
        raise HTTPException(status_code=404, detail="Connection not found") from None


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
# Dump
# ---------------------------------------------------------------------------

@router.get("/dump/{conn_id}")
async def dump(request: Request, conn_id: int, db: str, coll: str = ""):
    """
    A database, or one collection, as a mongodump-layout ZIP.

    Built into a spooled temporary file on a worker thread (in memory up to
    32 MB, then on disk) and streamed from there. The original held every
    document of every collection in a Python list and then the whole ZIP in a
    BytesIO, twice over in memory for a database of any size.
    """
    user_id = _require_user(request)
    client = _client(user_id, conn_id)

    def build():
        from bson import json_util
        from bson.codec_options import CodecOptions
        from bson.raw_bson import RawBSONDocument

        raw = CodecOptions(document_class=RawBSONDocument)
        database = client[db]
        if coll:
            specs = list(database.list_collections(filter={"name": coll}))
            if not specs:
                raise LookupError(f"Collection {coll!r} not found")
        else:
            specs = [
                spec for spec in database.list_collections()
                if spec.get("type", "collection") == "collection"
                and not spec["name"].startswith("system.")
            ]

        spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
            for spec in specs:
                name = spec["name"]
                collection = database.get_collection(name, codec_options=raw)
                with archive.open(f"{db}/{name}.bson", "w", force_zip64=True) as out:
                    for doc in collection.find():
                        out.write(doc.raw)
                metadata = {
                    "collectionName": name,
                    "type": spec.get("type", "collection"),
                    "options": spec.get("options", {}),
                    "indexes": list(database[name].list_indexes()),
                }
                archive.writestr(
                    f"{db}/{name}.metadata.json",
                    json_util.dumps(metadata, json_options=json_util.CANONICAL_JSON_OPTIONS),
                )
        size = spool.tell()
        spool.seek(0)
        return spool, size

    try:
        spool, size = await anyio.to_thread.run_sync(build)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        from services.mongo_pool import error_text

        raise HTTPException(status_code=400, detail=error_text(exc)) from None

    def chunks() -> Iterator[bytes]:
        try:
            while True:
                block = spool.read(_CHUNK)
                if not block:
                    break
                yield block
        finally:
            spool.close()

    headers = _attachment(f"{_safe_filename(db, coll)}.zip")
    headers["Content-Length"] = str(size)
    return StreamingResponse(chunks(), media_type="application/zip", headers=headers)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

@router.post("/restore/{conn_id}")
async def restore(request: Request, conn_id: int, mode: str = "skip", db: str = ""):
    """
    Restore a dump ZIP. The body is the ZIP itself (``application/zip``), not a
    multipart form: nothing needs parsing, and nothing is read until the
    caller is authenticated and the CSRF token checks out.

    ``db`` restores every collection in the archive into that one database;
    without it, each goes to the database named by its folder.

    Modes: ``skip`` keeps documents whose ``_id`` already exists, ``drop``
    empties each collection first, ``merge`` replaces by ``_id``.
    """
    user_id = _require_user(request)
    _require_csrf(request)
    if mode not in RESTORE_MODES:
        raise HTTPException(status_code=400, detail="mode must be skip, drop or merge")
    target_db = (db or "").strip()
    if target_db in SYSTEM_DATABASES:
        raise HTTPException(status_code=400, detail=f"{target_db} is a system database")
    client = _client(user_id, conn_id)

    limit = max_restore_bytes()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"Larger than the {limit:,}-byte limit")

    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    try:
        received = 0
        async for block in request.stream():
            received += len(block)
            if received > limit:
                raise HTTPException(status_code=413, detail=f"Larger than the {limit:,}-byte limit")
            await anyio.to_thread.run_sync(spool.write, block)
        spool.seek(0)
        if not zipfile.is_zipfile(spool):
            raise HTTPException(status_code=400, detail="That is not a ZIP file")
        spool.seek(0)
        summary = await anyio.to_thread.run_sync(
            restore_archive, client, spool, mode, target_db or None
        )
    finally:
        spool.close()
    return JSONResponse({"ok": True, **summary})


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


def restore_archive(client, archive_file, mode: str, target_db: Optional[str]) -> dict:
    import bson
    from bson import json_util
    from bson.codec_options import CodecOptions
    from bson.raw_bson import RawBSONDocument
    from pymongo import ReplaceOne
    from pymongo.errors import BulkWriteError

    raw = CodecOptions(document_class=RawBSONDocument)
    results: list[dict] = []
    with zipfile.ZipFile(archive_file) as archive:
        members = set(archive.namelist())
        jobs, skipped = plan_restore(sorted(members), target_db)
        for job in jobs:
            outcome = {
                "db": job["db"], "collection": job["collection"],
                "inserted": 0, "replaced": 0, "skipped": 0, "errors": [], "indexes": 0,
            }
            collection = client[job["db"]].get_collection(job["collection"], codec_options=raw)
            if mode == "drop":
                collection.drop()

            def flush(batch: list) -> None:
                if not batch:
                    return
                if mode == "merge":
                    result = collection.bulk_write(
                        [ReplaceOne({"_id": doc["_id"]}, doc, upsert=True) for doc in batch],
                        ordered=False,
                    )
                    outcome["inserted"] += result.upserted_count
                    outcome["replaced"] += result.matched_count
                    return
                try:
                    collection.insert_many(batch, ordered=False)
                    # Not len(result.inserted_ids): pymongo leaves that empty
                    # for RawBSONDocument inserts.
                    outcome["inserted"] += len(batch)
                except BulkWriteError as exc:
                    details = exc.details or {}
                    outcome["inserted"] += int(details.get("nInserted") or 0)
                    for error in details.get("writeErrors", []):
                        if error.get("code") == 11000 and mode == "skip":
                            outcome["skipped"] += 1
                        elif len(outcome["errors"]) < 5:
                            outcome["errors"].append(error.get("errmsg", "write error"))

            try:
                with archive.open(job["member"]) as stream:
                    batch: list = []
                    for doc in bson.decode_file_iter(stream, codec_options=raw):
                        batch.append(doc)
                        if len(batch) >= _BATCH:
                            flush(batch)
                            batch = []
                    flush(batch)
            except Exception as exc:  # noqa: BLE001 - reported per collection
                outcome["errors"].append(str(exc)[:300])

            if job["metadata"] in members:
                try:
                    metadata = json_util.loads(archive.read(job["metadata"]))
                    target = client[job["db"]][job["collection"]]
                    for index in metadata.get("indexes", []):
                        if index.get("name") == "_id_":
                            continue
                        options = {
                            key: value for key, value in index.items()
                            if key not in ("key", "v", "ns", "background")
                        }
                        target.create_index(list(dict(index["key"]).items()), **options)
                        outcome["indexes"] += 1
                except Exception as exc:  # noqa: BLE001
                    outcome["errors"].append(f"indexes: {str(exc)[:300]}")
            results.append(outcome)
    return {"collections": results, "skipped": skipped}
