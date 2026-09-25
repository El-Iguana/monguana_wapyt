"""
BFF: everything that talks to a MongoDB server.

Every export is a sync ``def``. pytincture runs sync exports on a worker
thread, so blocking pymongo calls are correct here, and the browser API stays
uniformly ``*_async`` (IguanaXterm's CLAUDE.md, "BFF calls use the generated
``*_async`` name").

Every export returns ``{"ok": True, ...}`` or ``{"ok": False, "error": ...}``
rather than raising: a raised exception reaches the browser as a 500 with a
correlation id, which tells the person nothing about their typo.

Queries arrive as **text** and are parsed here by ``mql`` — the browser never
builds BSON. Documents leave as relaxed Extended JSON (``mql.to_display``),
each paired with its ``_id`` in canonical Extended JSON so that edits and
deletes can name it exactly, whatever its type.

State that must persist (the client pool) lives in ``mongo_pool``; this module
is re-executed on every call.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid
from typing import Any, Callable

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id

# Hard caps. The original let a page size through up to 200 and an aggregate
# to 1000; the same numbers here, plus a server-side time limit so a filter on
# an unindexed field in a big collection cannot hold a worker thread forever.
MAX_PAGE_SIZE = 500
MAX_AGGREGATE_RESULTS = 1000
MAX_BULK_IDS = 1000
QUERY_TIME_LIMIT_MS = 30_000
COUNT_TIME_LIMIT_MS = 5_000
PREVIEW_DOCS = 20
SCHEMA_SAMPLE_SIZE = 200

SYSTEM_DATABASES = ("admin", "local", "config")

_DB_NAME_BAD = r'[/\\. "$*<>:|?\x00]'


class _Refused(Exception):
    """A request refused before it reached the server; its text is shown."""


@backend_for_frontend
@bff_policy(application="monguana")
class MongoService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _client(self, conn_id: int):
        if not self._user_id:
            raise _Refused("Not authenticated")
        from services.mongo_pool import ProfileNotFound, pool

        try:
            return pool.client(self._user_id, int(conn_id))
        except ProfileNotFound:
            raise _Refused("Connection not found") from None

    def _collection(self, conn_id: int, db: str, coll: str):
        _check_db_name(db)
        if not coll:
            raise _Refused("Collection name is required")
        return self._client(conn_id)[db][coll]

    def _guard(self, work: Callable[[], dict]) -> dict:
        from pymongo.errors import OperationFailure

        from services.mql import MQLError
        from services.mongo_pool import error_text

        try:
            result = work()
        except (_Refused, MQLError) as exc:
            return {"ok": False, "error": str(exc)}
        except OperationFailure as exc:
            details = exc.details or {}
            return {
                "ok": False,
                "error": details.get("errmsg") or error_text(exc),
                "code": exc.code,
            }
        except Exception as exc:  # noqa: BLE001 - the text is what they need
            return {"ok": False, "error": error_text(exc)}
        return {"ok": True, **result}

    # ------------------------------------------------------------------
    # Server, databases, collections
    # ------------------------------------------------------------------

    def server_info(self, conn_id: int) -> dict:
        def work() -> dict:
            client = self._client(conn_id)
            info = client.server_info()
            hello = client.admin.command("hello")
            return {
                "version": info.get("version", ""),
                "set_name": hello.get("setName", ""),
                "primary": bool(hello.get("isWritablePrimary", hello.get("ismaster"))),
                "hosts": hello.get("hosts", []),
            }

        return self._guard(work)

    def databases(self, conn_id: int) -> dict:
        """
        Every database, with its size. A user without ``listDatabases`` sees
        the databases they have privileges on (MongoDB filters with
        ``authorizedDatabases``), or failing that the profile's default.
        """
        def work() -> dict:
            from pymongo.errors import OperationFailure

            client = self._client(conn_id)
            try:
                listing = client.list_databases(authorizedDatabases=True)
                rows = [
                    {
                        "name": item["name"],
                        "size": int(item.get("sizeOnDisk") or 0),
                        "empty": bool(item.get("empty")),
                    }
                    for item in listing
                ]
            except OperationFailure:
                from services.db import fetch_connection

                profile = fetch_connection(int(conn_id), self._user_id) or {}
                default = profile.get("default_db") or ""
                if not default:
                    raise
                rows = [{"name": default, "size": 0, "empty": False}]
            rows.sort(key=lambda row: (row["name"] in SYSTEM_DATABASES, row["name"].lower()))
            return {"databases": rows}

        return self._guard(work)

    def collections(self, conn_id: int, db: str) -> dict:
        def work() -> dict:
            _check_db_name(db)
            database = self._client(conn_id)[db]
            rows = [
                {
                    "name": item["name"],
                    "type": item.get("type", "collection"),
                    "system": item["name"].startswith("system."),
                }
                for item in database.list_collections(authorizedCollections=True, nameOnly=True)
            ]
            rows.sort(key=lambda row: (row["system"], row["name"].lower()))
            return {"collections": rows}

        return self._guard(work)

    def create_database(self, conn_id: int, db: str, collection: str) -> dict:
        """MongoDB has no empty databases: one is created by its first collection."""
        def work() -> dict:
            name = (db or "").strip()
            _check_db_name(name)
            if name in SYSTEM_DATABASES:
                raise _Refused(f"{name} is a system database")
            first = (collection or "").strip()
            _check_collection_name(first)
            client = self._client(conn_id)
            if name in client.list_database_names():
                raise _Refused(f"Database {name!r} already exists")
            client[name].create_collection(first)
            return {"db": name, "collection": first}

        return self._guard(work)

    def drop_database(self, conn_id: int, db: str) -> dict:
        def work() -> dict:
            _check_db_name(db)
            if db in SYSTEM_DATABASES:
                raise _Refused(f"{db} is a system database")
            self._client(conn_id).drop_database(db)
            return {"dropped": db}

        return self._guard(work)

    def create_collection(
        self, conn_id: int, db: str, name: str, capped: bool = False,
        size: int = 0, max_docs: int = 0,
    ) -> dict:
        def work() -> dict:
            _check_db_name(db)
            coll = (name or "").strip()
            _check_collection_name(coll)
            options: dict = {}
            if capped:
                if int(size or 0) <= 0:
                    raise _Refused("A capped collection needs a size in bytes")
                options = {"capped": True, "size": int(size)}
                if int(max_docs or 0) > 0:
                    options["max"] = int(max_docs)
            self._client(conn_id)[db].create_collection(coll, **options)
            return {"db": db, "collection": coll}

        return self._guard(work)

    def drop_collection(self, conn_id: int, db: str, coll: str) -> dict:
        def work() -> dict:
            collection = self._collection(conn_id, db, coll)
            if not collection.database.list_collection_names(filter={"name": coll}):
                raise _Refused(f"Collection {coll!r} not found")
            collection.drop()
            return {"dropped": coll}

        return self._guard(work)

    def rename_collection(self, conn_id: int, db: str, coll: str, new_name: str) -> dict:
        def work() -> dict:
            target = (new_name or "").strip()
            _check_collection_name(target)
            if target == coll:
                raise _Refused("That is already its name")
            self._collection(conn_id, db, coll).rename(target, dropTarget=False)
            return {"db": db, "old": coll, "new": target}

        return self._guard(work)

    def stats(self, conn_id: int, db: str, coll: str) -> dict:
        """Counts and sizes, from ``$collStats`` (``collStats`` is deprecated)."""
        def work() -> dict:
            collection = self._collection(conn_id, db, coll)
            rows = list(collection.aggregate([{"$collStats": {"storageStats": {}}}]))
            storage = (rows[0] if rows else {}).get("storageStats", {})
            return {
                "count": int(storage.get("count") or 0),
                "size": int(storage.get("size") or 0),
                "avg_obj_size": int(storage.get("avgObjSize") or 0),
                "storage_size": int(storage.get("storageSize") or 0),
                "total_index_size": int(storage.get("totalIndexSize") or 0),
                "index_sizes": {k: int(v) for k, v in (storage.get("indexSizes") or {}).items()},
                "capped": bool(storage.get("capped")),
            }

        return self._guard(work)

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------

    def indexes(self, conn_id: int, db: str, coll: str) -> dict:
        def work() -> dict:
            from services import mql

            rows = []
            for info in self._collection(conn_id, db, coll).list_indexes():
                view = _index_view(info)
                rows.append({
                    **view,
                    "keys": mql.to_display(view["keys"]),
                    "partial": mql.to_display(view["partial"]) if view["partial"] is not None else None,
                    "options": mql.to_display(view["options"]),
                    # The shell-syntax text the edit form starts from.
                    "keys_text": _shell(view["keys"]),
                    "partial_text": _shell(view["partial"]) if view["partial"] is not None else "",
                    "options_text": _shell(view["options"]) if view["options"] else "",
                })
            return {"indexes": rows}

        return self._guard(work)

    def create_index(
        self, conn_id: int, db: str, coll: str, keys: str, name: str = "",
        unique: bool = False, sparse: bool = False, ttl_seconds: int = 0,
        partial: str = "", hidden: bool = False, options: str = "",
    ) -> dict:
        def work() -> dict:
            key_list, index_options = _index_spec(
                keys, name, unique, sparse, ttl_seconds, partial, hidden, options
            )
            created = self._collection(conn_id, db, coll).create_index(key_list, **index_options)
            return {"name": created}

        return self._guard(work)

    def update_index(
        self, conn_id: int, db: str, coll: str, name: str, keys: str,
        new_name: str = "", unique: bool = False, sparse: bool = False,
        ttl_seconds: int = 0, partial: str = "", hidden: bool = False,
        options: str = "", dry_run: bool = False,
    ) -> dict:
        """
        Change an existing index to match the given definition.

        MongoDB can change three things in place with ``collMod``: the TTL
        (set or change — not remove), hidden, and non-unique -> unique. Those
        are done in place, without rebuilding. Anything else — keys, name,
        sparse, partial filter, collation and other options, removing a TTL,
        unique -> non-unique (in place only from 7.1) — means a rebuild:

        * under a **new name**: build the new index first, then drop the old,
          so the collection is never without it;
        * under the **same name** (MongoDB cannot rename an index): drop, then
          build. If the build fails, the old index is recreated from its
          exact spec.

        ``dry_run`` returns the plan without touching anything; the UI shows
        it for confirmation.
        """
        def work() -> dict:
            if name == "_id_":
                raise _Refused("The _id index cannot be changed")
            collection = self._collection(conn_id, db, coll)
            info = next((item for item in collection.list_indexes() if item.get("name") == name), None)
            if info is None:
                raise _Refused(f"Index {name!r} no longer exists")
            current = _index_view(info)

            final_name = (new_name or "").strip() or name
            key_list, index_options = _index_spec(
                keys, final_name, unique, sparse, ttl_seconds, partial, hidden, options
            )
            desired = {
                "keys": dict(key_list),
                "name": final_name,
                "unique": bool(unique),
                "sparse": bool(sparse),
                "ttl": index_options.get("expireAfterSeconds"),
                "partial": index_options.get("partialFilterExpression"),
                "hidden": bool(hidden),
                "options": {k: v for k, v in index_options.items() if k in INDEX_EXTRA_OPTIONS},
            }

            rebuild: list[str] = []
            if list(desired["keys"].items()) != list(current["keys"].items()):
                rebuild.append("keys")
            if desired["name"] != current["name"]:
                rebuild.append(f"name → {final_name}")
            if desired["sparse"] != current["sparse"]:
                rebuild.append("sparse on" if desired["sparse"] else "sparse off")
            if not _same_bson(desired["partial"], current["partial"]):
                rebuild.append("partial filter")
            if not _same_bson(desired["options"], current["options"]):
                rebuild.append("options")
            if current["ttl"] is not None and desired["ttl"] is None:
                rebuild.append("TTL removed")
            if current["unique"] and not desired["unique"]:
                rebuild.append("unique off")

            in_place: list[tuple[str, dict]] = []
            if desired["hidden"] != current["hidden"]:
                in_place.append(("hidden" if desired["hidden"] else "unhidden",
                                 {"hidden": desired["hidden"]}))
            if desired["ttl"] is not None and desired["ttl"] != current["ttl"]:
                in_place.append((f"TTL {desired['ttl']}s", {"expireAfterSeconds": desired["ttl"]}))
            if desired["unique"] and not current["unique"]:
                in_place.append(("unique on", {"unique": True}))

            if rebuild:
                strategy = "build-then-drop" if final_name != name else "drop-then-build"
                changes = rebuild + [label for label, _ in in_place]
            elif in_place:
                strategy, changes = "in-place", [label for label, _ in in_place]
            else:
                strategy, changes = "none", []

            if dry_run or strategy == "none":
                return {"strategy": strategy, "changes": changes, "name": name}

            if strategy == "in-place":
                for _label, change in in_place:
                    _coll_mod(collection, name, change)
                return {"strategy": strategy, "changes": changes, "name": name}

            strategy = _rebuild_index(collection, info, key_list, index_options, strategy)
            return {"strategy": strategy, "changes": changes, "name": final_name}

        return self._guard(work)

    def set_index_hidden(self, conn_id: int, db: str, coll: str, name: str, hidden: bool) -> dict:
        """Hide an index from the planner (it is still maintained), or unhide it."""
        def work() -> dict:
            if name == "_id_":
                raise _Refused("The _id index cannot be hidden")
            _coll_mod(self._collection(conn_id, db, coll), name, {"hidden": bool(hidden)})
            return {"name": name, "hidden": bool(hidden)}

        return self._guard(work)

    def drop_index(self, conn_id: int, db: str, coll: str, name: str) -> dict:
        def work() -> dict:
            if name == "_id_":
                raise _Refused("The _id index cannot be dropped")
            self._collection(conn_id, db, coll).drop_index(name)
            return {"dropped": name}

        return self._guard(work)

    # ------------------------------------------------------------------
    # Schema sampling
    # ------------------------------------------------------------------

    def schema(self, conn_id: int, db: str, coll: str, sample: int = SCHEMA_SAMPLE_SIZE) -> dict:
        """
        Field paths and their types across a random sample.

        Probabilistic by nature: a field present in one document in a million
        will usually be missing. The UI says so and offers a refresh.
        """
        def work() -> dict:
            size = max(1, min(int(sample or SCHEMA_SAMPLE_SIZE), 1000))
            collection = self._collection(conn_id, db, coll)
            fields: dict[str, dict] = {}
            seen = 0
            for doc in collection.aggregate(
                [{"$sample": {"size": size}}], maxTimeMS=QUERY_TIME_LIMIT_MS
            ):
                seen += 1
                _collect_paths(doc, "", fields, depth=0)
            rows = [
                {"path": path, "types": sorted(info["types"]), "count": info["count"]}
                for path, info in fields.items()
            ]
            rows.sort(key=lambda row: (row["path"] != "_id", row["path"].lower()))
            return {"fields": rows, "sampled": seen}

        return self._guard(work)

    # ------------------------------------------------------------------
    # Reading documents
    # ------------------------------------------------------------------

    def find(
        self, conn_id: int, db: str, coll: str, filter: str = "", sort: str = "",
        projection: str = "", page: int = 1, page_size: int = 50,
    ) -> dict:
        def work() -> dict:
            from pymongo.errors import ExecutionTimeout

            from services import mql

            query = mql.parse_object(filter, "filter")
            mql.check_query(query)
            order = mql.parse_object(sort, "sort")
            fields = mql.parse_object(projection, "projection") or None
            if fields:
                mql.check_query(fields)
            size = max(1, min(int(page_size or 50), MAX_PAGE_SIZE))
            number = max(1, int(page or 1))

            collection = self._collection(conn_id, db, coll)
            cursor = collection.find(query, fields, max_time_ms=QUERY_TIME_LIMIT_MS)
            if order:
                cursor = cursor.sort(list(order.items()))
            docs = list(cursor.skip((number - 1) * size).limit(size))

            # An exact count can be slower than the page itself on a big
            # collection with an unindexed filter, so it gets a short leash;
            # past that, an unfiltered view falls back to the metadata count
            # and a filtered one admits it does not know.
            exact = True
            try:
                total = collection.count_documents(query, maxTimeMS=COUNT_TIME_LIMIT_MS)
            except ExecutionTimeout:
                exact = False
                total = collection.estimated_document_count() if not query else None

            return {
                "docs": [_row(doc) for doc in docs],
                "total": total,
                "exact": exact,
                "page": number,
                "page_size": size,
                # So the table can put its header caret where the server
                # sorted; a {$meta: ...} direction reads as 0.
                "sort": [[key, value if value in (1, -1) else 0] for key, value in order.items()],
            }

        return self._guard(work)

    def count(self, conn_id: int, db: str, coll: str, filter: str = "") -> dict:
        def work() -> dict:
            from services import mql

            query = mql.parse_object(filter, "filter")
            mql.check_query(query)
            total = self._collection(conn_id, db, coll).count_documents(
                query, maxTimeMS=QUERY_TIME_LIMIT_MS
            )
            return {"total": total}

        return self._guard(work)

    def explain(self, conn_id: int, db: str, coll: str, filter: str = "", sort: str = "") -> dict:
        def work() -> dict:
            from services import mql

            query = mql.parse_object(filter, "filter")
            mql.check_query(query)
            order = mql.parse_object(sort, "sort")
            cursor = self._collection(conn_id, db, coll).find(query)
            if order:
                cursor = cursor.sort(list(order.items()))
            plan = cursor.explain()
            winning = (plan.get("queryPlanner") or {}).get("winningPlan") or {}
            stats = plan.get("executionStats") or {}
            return {
                "summary": _plan_summary(winning),
                "docs_examined": stats.get("totalDocsExamined"),
                "keys_examined": stats.get("totalKeysExamined"),
                "returned": stats.get("nReturned"),
                "ms": stats.get("executionTimeMillis"),
                "plan": mql.to_display(plan.get("queryPlanner") or plan),
            }

        return self._guard(work)

    def get_document(self, conn_id: int, db: str, coll: str, doc_id: str) -> dict:
        """
        The whole document. The editor always starts from this, not from the
        row on screen: a projection may have hidden fields, and saving a
        projected copy would delete them.
        """
        def work() -> dict:
            from services import mql

            doc = self._collection(conn_id, db, coll).find_one({"_id": mql.decode_id(doc_id)})
            if doc is None:
                raise _Refused("That document no longer exists")
            return {"doc": mql.to_display(doc), "id": doc_id}

        return self._guard(work)

    def aggregate(self, conn_id: int, db: str, coll: str, pipeline: str) -> dict:
        def work() -> dict:
            from services import mql

            stages = mql.parse_pipeline(pipeline)
            cursor = self._collection(conn_id, db, coll).aggregate(
                [*stages, {"$limit": MAX_AGGREGATE_RESULTS + 1}],
                maxTimeMS=QUERY_TIME_LIMIT_MS,
            )
            docs = list(cursor)
            truncated = len(docs) > MAX_AGGREGATE_RESULTS
            docs = docs[:MAX_AGGREGATE_RESULTS]
            return {"docs": [_row(doc) for doc in docs], "truncated": truncated}

        return self._guard(work)

    # ------------------------------------------------------------------
    # Writing documents
    # ------------------------------------------------------------------

    def insert(self, conn_id: int, db: str, coll: str, text: str) -> dict:
        """One document, or an array of them."""
        def work() -> dict:
            from services import mql

            value = mql.parse(text or "")
            docs = value if isinstance(value, list) else [value]
            if not docs or not all(isinstance(doc, dict) for doc in docs):
                raise _Refused("Give a document {…} or an array of documents [{…}, {…}]")
            collection = self._collection(conn_id, db, coll)
            if len(docs) == 1:
                inserted = [collection.insert_one(docs[0]).inserted_id]
            else:
                inserted = collection.insert_many(docs, ordered=True).inserted_ids
            return {"ids": [mql.encode_id(item) for item in inserted], "count": len(inserted)}

        return self._guard(work)

    def replace(self, conn_id: int, db: str, coll: str, doc_id: str, text: str) -> dict:
        """
        Save an edited document whole.

        A replacement, not the original's ``$set`` of the edited body: with
        ``$set`` a field deleted in the editor stayed in the database.
        """
        def work() -> dict:
            from services import mql

            original = mql.decode_id(doc_id)
            doc = mql.parse_object(text, "document")
            if "_id" in doc and doc["_id"] != original:
                raise _Refused("The _id cannot be changed. Use Clone to copy a document.")
            doc.pop("_id", None)
            result = self._collection(conn_id, db, coll).replace_one({"_id": original}, doc)
            if result.matched_count == 0:
                raise _Refused("That document no longer exists")
            return {"modified": result.modified_count}

        return self._guard(work)

    def delete_document(self, conn_id: int, db: str, coll: str, doc_id: str) -> dict:
        def work() -> dict:
            from services import mql

            result = self._collection(conn_id, db, coll).delete_one(
                {"_id": mql.decode_id(doc_id)}
            )
            if result.deleted_count == 0:
                raise _Refused("That document no longer exists")
            return {"deleted": 1}

        return self._guard(work)

    def preview_write(self, conn_id: int, db: str, coll: str, filter: str = "", multi: bool = True) -> dict:
        """What an updateOne/updateMany/delete would touch, before it does."""
        def work() -> dict:
            from services import mql

            query = mql.parse_object(filter, "filter")
            mql.check_query(query)
            collection = self._collection(conn_id, db, coll)
            if multi:
                matched = collection.count_documents(query, maxTimeMS=QUERY_TIME_LIMIT_MS)
            else:
                matched = collection.count_documents(query, limit=1)
            docs = list(collection.find(query, max_time_ms=QUERY_TIME_LIMIT_MS)
                        .limit(PREVIEW_DOCS if multi else 1))
            return {"matched": matched, "docs": [_row(doc) for doc in docs]}

        return self._guard(work)

    def update_where(
        self, conn_id: int, db: str, coll: str, filter: str, update: str,
        multi: bool = False, upsert: bool = False,
    ) -> dict:
        def work() -> dict:
            from services import mql

            query = mql.parse_object(filter, "filter")
            mql.check_query(query)
            change = mql.parse_update(update)
            collection = self._collection(conn_id, db, coll)
            if multi:
                result = collection.update_many(query, change, upsert=bool(upsert))
            else:
                result = collection.update_one(query, change, upsert=bool(upsert))
            return {
                "matched": result.matched_count,
                "modified": result.modified_count,
                "upserted": mql.encode_id(result.upserted_id)
                if result.upserted_id is not None else None,
            }

        return self._guard(work)

    def delete_where(
        self, conn_id: int, db: str, coll: str, filter: str, multi: bool = True,
        allow_all: bool = False,
    ) -> dict:
        def work() -> dict:
            from services import mql

            query = mql.parse_object(filter, "filter")
            mql.check_query(query)
            if not query and not allow_all:
                raise _Refused("An empty filter matches every document; confirm to delete them all")
            collection = self._collection(conn_id, db, coll)
            result = collection.delete_many(query) if multi else collection.delete_one(query)
            return {"deleted": result.deleted_count}

        return self._guard(work)

    def bulk_delete(self, conn_id: int, db: str, coll: str, ids: list) -> dict:
        def work() -> dict:
            decoded = _decode_ids(ids)
            result = self._collection(conn_id, db, coll).delete_many({"_id": {"$in": decoded}})
            return {"deleted": result.deleted_count}

        return self._guard(work)

    def bulk_update(self, conn_id: int, db: str, coll: str, ids: list, update: str) -> dict:
        """Any update operators, not only the original's ``$set``."""
        def work() -> dict:
            from services import mql

            decoded = _decode_ids(ids)
            change = mql.parse_update(update)
            result = self._collection(conn_id, db, coll).update_many(
                {"_id": {"$in": decoded}}, change
            )
            return {"matched": result.matched_count, "modified": result.modified_count}

        return self._guard(work)


# ----------------------------------------------------------------------
# Module-level helpers. Functions only: anything stateful here would be
# rebuilt on every call (see mongo_pool's docstring).
# ----------------------------------------------------------------------

def _check_db_name(name: str) -> None:
    if not name or not name.strip():
        raise _Refused("Database name is required")
    if re.search(_DB_NAME_BAD, name) or len(name.encode()) > 63:
        raise _Refused("Database names cannot contain / \\ . space \" $ * < > : | ? and max 63 bytes")


def _check_collection_name(name: str) -> None:
    if not name:
        raise _Refused("Collection name is required")
    if name.startswith("system."):
        raise _Refused("Collection names cannot start with 'system.'")
    if "$" in name or "\x00" in name or len(name.encode()) > 255:
        raise _Refused("Collection names cannot contain $ and max 255 bytes")


# Index options accepted through the free-form "Other options" box, beyond the
# ones with their own fields. Anything else (storageEngine, v, …) is refused.
INDEX_EXTRA_OPTIONS = (
    "collation", "weights", "default_language", "language_override",
    "wildcardProjection", "2dsphereIndexVersion", "bits", "min", "max",
)

_INDEX_KEY_KINDS = ("text", "2dsphere", "2d", "hashed")


def _index_spec(keys, name, unique, sparse, ttl_seconds, partial, hidden, options):
    """The key list and create_index options for a definition typed in the UI."""
    from services import mql

    spec = mql.parse_object(keys, "index key")
    if not spec:
        raise _Refused("Give at least one key, like {email: 1}")
    for field, direction in spec.items():
        if direction not in (1, -1) and direction not in _INDEX_KEY_KINDS:
            raise _Refused(f"{field}: use 1, -1, 'text', '2dsphere', '2d' or 'hashed'"
                           " (a wildcard index is {\"$**\": 1})")
    result: dict = {}
    if (name or "").strip():
        result["name"] = name.strip()
    if unique:
        result["unique"] = True
    if sparse:
        result["sparse"] = True
    ttl = int(ttl_seconds or 0)
    if ttl < 0:
        raise _Refused("TTL seconds cannot be negative")
    if ttl > 0:
        result["expireAfterSeconds"] = ttl
    if (partial or "").strip():
        expression = mql.parse_object(partial, "partial filter")
        mql.check_query(expression)
        result["partialFilterExpression"] = expression
    if hidden:
        result["hidden"] = True
    extra = mql.parse_object(options, "options") if (options or "").strip() else {}
    unknown = [key for key in extra if key not in INDEX_EXTRA_OPTIONS]
    if unknown:
        raise _Refused(f"Option {unknown[0]!r} is not supported here; use one of "
                       + ", ".join(INDEX_EXTRA_OPTIONS))
    result.update(extra)
    return list(spec.items()), result


def _index_view(info: dict) -> dict:
    """
    An existing index as the edit form shows it — and as ``update_index``
    compares it. A text index is stored as ``{_fts: "text", _ftsx: 1}`` plus
    server-filled defaults (language, version, weights of 1); those are turned
    back into what was typed, or every edit of one would look like a change.
    """
    raw_keys = dict(info.get("key", {}))
    weights = dict(info.get("weights") or {})
    keys: dict = {}
    for field, direction in raw_keys.items():
        if field == "_fts":
            keys.update({text_field: "text" for text_field in weights})
        elif field != "_ftsx":
            keys[field] = direction

    options = {key: info[key] for key in INDEX_EXTRA_OPTIONS if key in info}
    if "_fts" in raw_keys:
        if all(value == 1 for value in weights.values()):
            options.pop("weights", None)
        if options.get("default_language") == "english":
            options.pop("default_language")
        if options.get("language_override") == "language":
            options.pop("language_override")
    options.pop("2dsphereIndexVersion", None)  # server-assigned
    ttl = info.get("expireAfterSeconds")
    return {
        "name": info.get("name", ""),
        "keys": keys,
        "unique": bool(info.get("unique")),
        "sparse": bool(info.get("sparse")),
        "ttl": int(ttl) if ttl is not None else None,
        "partial": info.get("partialFilterExpression"),
        "hidden": bool(info.get("hidden")),
        "options": options,
    }


def _shell(value: Any) -> str:
    """
    A BSON value as text the query parser reads back **exactly**: relaxed
    Extended JSON. Not the table's compact display, which shows a date as a
    bare ISO string and a decimal as a bare number and would change their
    types on the way back.
    """
    import json

    from services import mql

    return json.dumps(mql.to_display(value), ensure_ascii=False, separators=(", ", ": "))


def _same_bson(left: Any, right: Any) -> bool:
    """
    Equality through canonical Extended JSON. ``list_indexes`` returns naive
    datetimes even from a tz-aware client, so a partial filter read back from
    the edit form (aware) never compared equal and every edit looked like a
    rebuild. Canonical JSON writes both as the same UTC instant.
    """
    from services import mql

    return mql.encode_id(left) == mql.encode_id(right)


def _coll_mod(collection, name: str, change: dict) -> None:
    """
    One in-place index change. ``unique: True`` is the two-step MongoDB
    requires (``prepareUnique``, then ``unique``); if existing documents
    collide, the preparation is undone and the error names some of them.
    """
    from pymongo.errors import OperationFailure

    database = collection.database
    if change.get("unique"):
        database.command({"collMod": collection.name, "index": {"name": name, "prepareUnique": True}})
        try:
            database.command({"collMod": collection.name, "index": {"name": name, "unique": True}})
        except OperationFailure as exc:
            database.command({"collMod": collection.name,
                              "index": {"name": name, "prepareUnique": False}})
            if exc.code == 359:
                from services import docfmt, mql

                ids = [item for violation in (exc.details or {}).get("violations", [])
                       for item in violation.get("ids", [])]
                sample = ", ".join(docfmt.scalar_text(mql.to_display(item)) for item in ids[:5])
                raise _Refused(
                    f"{len(ids)} document(s) share a key, so the index cannot become "
                    f"unique. First: {sample}" if ids else
                    "Existing documents share a key, so the index cannot become unique."
                ) from None
            raise
        return
    database.command({"collMod": collection.name, "index": {"name": name, **change}})


def _rebuild_index(collection, info: dict, key_list: list, options: dict, strategy: str) -> str:
    """
    Replace an index. Returns the strategy actually used: build-then-drop can
    fall back to drop-then-build when MongoDB refuses two indexes with the
    same keys (codes 85/86).
    """
    from pymongo.errors import OperationFailure

    old_name = info["name"]
    if strategy == "build-then-drop":
        try:
            collection.create_index(key_list, **options)
        except OperationFailure as exc:
            if exc.code not in (85, 86):
                raise
            strategy = "drop-then-build"
        else:
            collection.drop_index(old_name)
            return strategy

    # Drop, then build; on failure put the old one back exactly as it was.
    old_keys = list(dict(info["key"]).items())
    old_options = {key: value for key, value in info.items() if key not in ("v", "key", "ns")}
    collection.drop_index(old_name)
    try:
        collection.create_index(key_list, **options)
    except Exception as exc:  # noqa: BLE001 - restore, then report
        try:
            collection.create_index(old_keys, **old_options)
        except Exception as restore_exc:  # noqa: BLE001
            raise _Refused(
                f"The new index failed ({_error(exc)}) and the old one could not be "
                f"restored ({_error(restore_exc)}). Recreate {old_name!r} by hand."
            ) from None
        raise _Refused(f"The new index failed, so {old_name!r} was restored: {_error(exc)}") from None
    return strategy


def _error(exc: Exception) -> str:
    from services.mongo_pool import error_text

    details = getattr(exc, "details", None) or {}
    return details.get("errmsg") or error_text(exc)


def _row(doc: Any) -> dict:
    from services import mql

    if isinstance(doc, dict) and "_id" in doc:
        return {"id": mql.encode_id(doc["_id"]), "doc": mql.to_display(doc)}
    return {"id": None, "doc": mql.to_display(doc)}


def _decode_ids(ids: Any) -> list:
    from services import mql

    if not isinstance(ids, list) or not ids:
        raise _Refused("Select at least one document")
    if len(ids) > MAX_BULK_IDS:
        raise _Refused(f"At most {MAX_BULK_IDS} documents at a time")
    return [mql.decode_id(str(item)) for item in ids]


def _bson_type(value: Any) -> str:
    from bson.binary import Binary
    from bson.decimal128 import Decimal128
    from bson.int64 import Int64
    from bson.objectid import ObjectId
    from bson.regex import Regex
    from bson.timestamp import Timestamp

    if value is None:
        return "Null"
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, Int64):
        return "Int64"
    if isinstance(value, int):
        return "Int32"
    if isinstance(value, float):
        return "Double"
    if isinstance(value, str):
        return "String"
    if isinstance(value, ObjectId):
        return "ObjectId"
    if isinstance(value, _dt.datetime):
        return "Date"
    if isinstance(value, Decimal128):
        return "Decimal128"
    if isinstance(value, (Binary, bytes, uuid.UUID)):
        return "Binary"
    if isinstance(value, (Regex, re.Pattern)):
        return "Regex"
    if isinstance(value, Timestamp):
        return "Timestamp"
    if isinstance(value, list):
        return "Array"
    if isinstance(value, dict):
        return "Object"
    return type(value).__name__


def _collect_paths(value: dict, prefix: str, fields: dict, depth: int) -> None:
    """Dotted paths with their types; array elements that are objects too."""
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        entry = fields.setdefault(path, {"types": set(), "count": 0})
        entry["types"].add(_bson_type(child))
        entry["count"] += 1
        if depth >= 8:
            continue
        if isinstance(child, dict):
            _collect_paths(child, path, fields, depth + 1)
        elif isinstance(child, list):
            for item in child[:20]:
                if isinstance(item, dict):
                    _collect_paths(item, path, fields, depth + 1)


def _plan_summary(stage: dict) -> str:
    """``FETCH ← IXSCAN {email: 1}``, reading the winning plan from the top."""
    parts = []
    current = stage
    while isinstance(current, dict) and current:
        name = current.get("stage") or next(iter(current), "?")
        if name == "IXSCAN" and current.get("keyPattern"):
            keys = ", ".join(f"{k}: {v}" for k, v in current["keyPattern"].items())
            name = f"IXSCAN {{{keys}}}"
        parts.append(str(name))
        current = current.get("inputStage") or (current.get("inputStages") or [None])[0] \
            or current.get("queryPlan")
    return " ← ".join(parts) or "?"
