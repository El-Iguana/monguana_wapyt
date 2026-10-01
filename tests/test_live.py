"""
The BFF services against a real MongoDB.

Skipped unless ``MONGUANA_TEST_MONGO`` names one, as ``host:port:user:password``
(user and password optional). The throwaway container used in development:

    podman run -d --name monguana-mongotest -p 127.0.0.1:27018:27017 \\
      -e MONGO_INITDB_ROOT_USERNAME=root \\
      -e 'MONGO_INITDB_ROOT_PASSWORD=p@ss:w/rd' docker.io/library/mongo:7
    MONGUANA_TEST_MONGO='127.0.0.1:27018:root:p@ss:w/rd' uv run pytest tests/test_live.py

The password has ``@``, ``:`` and ``/`` in it on purpose: the original built
its connection URI by string formatting and could not log in with one.
"""
from __future__ import annotations

import io
import os
import uuid
import zipfile

import pytest

SPEC = os.environ.get("MONGUANA_TEST_MONGO", "")
pytestmark = pytest.mark.skipif(not SPEC, reason="MONGUANA_TEST_MONGO is not set")

DB = f"monguana_test_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from services import db

    data = tmp_path_factory.mktemp("data")
    saved = (db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet)
    db.DATA_DIR, db.DB_PATH = data, data / "monguana.db"
    db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = data / "secret.key", data / "session.key", None
    db.init_db()

    from services.connection_service import ConnectionService
    from services.mongo_service import MongoService

    host, port, *rest = SPEC.split(":", 3)
    user = {"user_id": 1, "username": "admin", "is_admin": True}
    saved_conn = ConnectionService(user).save(
        name="test", host=host, port=int(port),
        username=rest[0] if rest else "", password=rest[1] if len(rest) > 1 else "",
    )
    assert saved_conn["ok"], saved_conn
    conn_id = saved_conn["id"]
    mongo = MongoService(user)
    yield {"conn": conn_id, "mongo": mongo, "conns": ConnectionService(user), "user": user}

    mongo.drop_database(conn_id, DB)
    from services.mongo_pool import pool

    pool.close_all()
    db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = saved


def ok(result: dict) -> dict:
    assert result.get("ok"), result
    return result


def test_the_password_with_special_characters_logs_in(env):
    result = env["conns"].test(conn_id=env["conn"], host=SPEC.split(":")[0],
                               port=int(SPEC.split(":")[1]),
                               username=SPEC.split(":", 3)[2] if SPEC.count(":") >= 2 else "")
    assert result["ok"], result
    assert result["version"]


def test_secrets_never_reach_the_browser(env):
    listed = env["conns"].list()[0]
    assert "password" not in listed and "uri" not in listed
    assert listed["has_password"] is bool(SPEC.count(":") >= 3)


def test_another_user_cannot_use_the_profile(env):
    from services.mongo_service import MongoService

    stranger = MongoService({"user_id": 999})
    assert stranger.databases(env["conn"]) == {"ok": False, "error": "Connection not found"}


def test_database_and_collection_lifecycle(env):
    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.create_database(conn, DB, "people"))
    names = [row["name"] for row in ok(mongo.databases(conn))["databases"]]
    assert DB in names
    ok(mongo.create_collection(conn, DB, "scratch"))
    ok(mongo.rename_collection(conn, DB, "scratch", "scratch2"))
    colls = [row["name"] for row in ok(mongo.collections(conn, DB))["collections"]]
    assert "scratch2" in colls and "scratch" not in colls
    ok(mongo.drop_collection(conn, DB, "scratch2"))
    assert not mongo.create_database(conn, "admin", "x")["ok"]
    assert "system" in mongo.drop_database(conn, "admin")["error"]


def test_documents_keep_their_types(env):
    mongo, conn = env["mongo"], env["conn"]
    inserted = ok(mongo.insert(conn, DB, "people", """[
        {_id: "alice", age: 31, joined: ISODate("2020-01-05T00:00:00Z"), tags: ["a", "b"]},
        {_id: 7, age: 45, joined: ISODate("2021-06-01T00:00:00Z"), ref: ObjectId("65a1b2c3d4e5f60718293a4b")},
        {name: "carol", age: 22, joined: new Date("2023-03-03"), key: UUID("12345678-1234-5678-1234-567812345678")},
    ]"""))
    assert inserted["count"] == 3

    # A filter on a date and an ObjectId: plain JSON could not express either.
    found = ok(mongo.find(conn, DB, "people", '{joined: {$gte: ISODate("2021-01-01")}}', "{age: 1}"))
    assert [row["doc"]["age"] for row in found["docs"]] == [22, 45]
    assert found["total"] == 2 and found["sort"] == [["age", 1]]
    by_ref = ok(mongo.find(conn, DB, "people", '{ref: ObjectId("65a1b2c3d4e5f60718293a4b")}'))
    assert by_ref["docs"][0]["doc"]["_id"] == 7

    projected = ok(mongo.find(conn, DB, "people", "{_id: 'alice'}", "", "{age: 1}"))
    assert set(projected["docs"][0]["doc"]) == {"_id", "age"}


def test_edit_with_a_string_id_and_the_editor_round_trip(env):
    import json

    mongo, conn = env["mongo"], env["conn"]
    row = ok(mongo.find(conn, DB, "people", "{_id: 'alice'}"))["docs"][0]
    full = ok(mongo.get_document(conn, DB, "people", row["id"]))["doc"]
    full["age"] = 32
    del full["tags"]  # a deleted field must really go (the original $set it back)
    ok(mongo.replace(conn, DB, "people", row["id"], json.dumps(full)))
    after = ok(mongo.get_document(conn, DB, "people", row["id"]))["doc"]
    assert after["age"] == 32 and "tags" not in after
    assert after["joined"] == {"$date": "2020-01-05T00:00:00Z"}, "the date stayed a date"

    changed = dict(after, _id="bob")
    refused = mongo.replace(conn, DB, "people", row["id"], json.dumps(changed))
    assert not refused["ok"] and "_id cannot be changed" in refused["error"]


def test_numeric_id_delete(env):
    mongo, conn = env["mongo"], env["conn"]
    row = ok(mongo.find(conn, DB, "people", "{_id: 7}"))["docs"][0]
    ok(mongo.delete_document(conn, DB, "people", row["id"]))
    assert not mongo.delete_document(conn, DB, "people", row["id"])["ok"]


def test_query_mode_writes_preview_then_apply(env):
    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.insert(conn, DB, "jobs", "[{s: 'new', n: 1}, {s: 'new', n: 2}, {s: 'done', n: 3}]"))
    preview = ok(mongo.preview_write(conn, DB, "jobs", "{s: 'new'}", True))
    assert preview["matched"] == 2 and len(preview["docs"]) == 2
    one = ok(mongo.preview_write(conn, DB, "jobs", "{s: 'new'}", False))
    assert one["matched"] == 1 and len(one["docs"]) == 1

    result = ok(mongo.update_where(conn, DB, "jobs", "{s: 'new'}", "{$inc: {n: 10}}", True))
    assert result["modified"] == 2
    refused = mongo.update_where(conn, DB, "jobs", "{}", "{s: 'x'}", True)
    assert not refused["ok"] and "operators" in refused["error"]

    assert not mongo.delete_where(conn, DB, "jobs", "{}", True)["ok"], "empty filter needs allow_all"
    assert ok(mongo.delete_where(conn, DB, "jobs", "{s: 'done'}", True))["deleted"] == 1


def test_bulk_by_selection(env):
    mongo, conn = env["mongo"], env["conn"]
    ids = [row["id"] for row in ok(mongo.find(conn, DB, "jobs"))["docs"]]
    assert ok(mongo.bulk_update(conn, DB, "jobs", ids, "{$set: {s: 'bulk'}, $unset: {n: ''}}"))["modified"] == 2
    assert ok(mongo.count(conn, DB, "jobs", "{s: 'bulk', n: {$exists: false}}"))["total"] == 2
    assert ok(mongo.bulk_delete(conn, DB, "jobs", ids))["deleted"] == 2


def test_javascript_never_reaches_the_server(env):
    mongo, conn = env["mongo"], env["conn"]
    result = mongo.find(conn, DB, "people", '{$or: [{$where: "true"}]}')
    assert not result["ok"] and "not permitted" in result["error"]
    result = mongo.aggregate(conn, DB, "people", '[{$lookup: {from: "x", as: "y", pipeline: [{$out: "z"}]}}]')
    assert not result["ok"] and "not permitted" in result["error"]


def test_aggregate_indexes_schema_explain_stats(env):
    mongo, conn = env["mongo"], env["conn"]
    agg = ok(mongo.aggregate(conn, DB, "people", "[{$group: {_id: null, n: {$sum: 1}}}]"))
    assert agg["docs"][0]["doc"] == {"_id": None, "n": 2}
    assert agg["docs"][0]["id"] is not None  # an _id of null is still an _id

    ok(mongo.create_index(conn, DB, "people", "{age: -1}", "by_age", unique=False))
    ok(mongo.create_index(conn, DB, "people", "{name: 1}", "", unique=True,
                          partial="{age: {$gt: 18}}"))
    indexes = {row["name"]: row for row in ok(mongo.indexes(conn, DB, "people"))["indexes"]}
    assert indexes["by_age"]["keys"] == {"age": -1}
    assert indexes["name_1"]["partial"] == {"age": {"$gt": 18}}
    assert not mongo.drop_index(conn, DB, "people", "_id_")["ok"]
    ok(mongo.drop_index(conn, DB, "people", "by_age"))

    schema = ok(mongo.schema(conn, DB, "people"))
    types = {row["path"]: row["types"] for row in schema["fields"]}
    assert types["joined"] == ["Date"] and "Binary" in types["key"]

    plan = ok(mongo.explain(conn, DB, "people", "{name: 'carol', age: {$gt: 18}}"))
    assert "IXSCAN {name: 1}" in plan["summary"], plan["summary"]
    assert plan["returned"] == 1
    assert ok(mongo.stats(conn, DB, "people"))["count"] == 2


def test_restore_archive_modes(env):
    import bson

    from services.mongo_pool import pool
    from services.transfer import restore_archive

    client = pool.client(1, env["conn"])
    target = f"{DB}_restored"
    docs = [{"_id": i, "v": i} for i in range(3)]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("src/things.bson", b"".join(bson.encode(doc) for doc in docs))
        archive.writestr(
            "src/things.metadata.json",
            '{"indexes": [{"v": 2, "key": {"_id": 1}, "name": "_id_"},'
            ' {"v": 2, "key": {"v": 1}, "name": "v_1", "unique": true}]}',
        )
    try:
        buffer.seek(0)
        first = restore_archive(client, buffer, "skip", target)["collections"][0]
        assert (first["inserted"], first["indexes"]) == (3, 1)
        buffer.seek(0)
        again = restore_archive(client, buffer, "skip", target)["collections"][0]
        assert (again["inserted"], again["skipped"]) == (0, 3)
        client[target]["things"].update_one({"_id": 1}, {"$set": {"v": 99}})
        buffer.seek(0)
        merged = restore_archive(client, buffer, "merge", target)["collections"][0]
        assert merged["replaced"] == 3
        assert client[target]["things"].find_one({"_id": 1})["v"] == 1
        buffer.seek(0)
        dropped = restore_archive(client, buffer, "drop", target)["collections"][0]
        assert dropped["inserted"] == 3
    finally:
        client.drop_database(target)


# -- editing indexes ----------------------------------------------------------

def _index(mongo, conn, name):
    rows = ok(mongo.indexes(conn, DB, "idx"))["indexes"]
    return next((row for row in rows if row["name"] == name), None)


def _edit(mongo, conn, name, row, **changes):
    """update_index with the row's current definition plus ``changes``."""
    args = dict(keys=row["keys_text"], new_name="", unique=row["unique"], sparse=row["sparse"],
                ttl_seconds=row["ttl"] or 0, partial=row["partial_text"],
                hidden=row["hidden"], options=row["options_text"])
    args.update(changes)
    return mongo.update_index(conn, DB, "idx", name, **args)


def test_index_edits_in_place(env):
    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.insert(conn, DB, "idx", "[{v: 1, w: 1}, {v: 1, w: 2}, {v: 2, w: 3}]"))
    ok(mongo.create_index(conn, DB, "idx", "{w: 1}", ""))

    row = _index(mongo, conn, "w_1")
    assert _edit(mongo, conn, "w_1", row, dry_run=True)["strategy"] == "none"
    plan = ok(_edit(mongo, conn, "w_1", row, ttl_seconds=3600, dry_run=True))
    assert plan["strategy"] == "in-place" and plan["changes"] == ["TTL 3600s"]
    assert _index(mongo, conn, "w_1")["ttl"] is None, "a dry run changes nothing"

    ok(_edit(mongo, conn, "w_1", row, ttl_seconds=3600, unique=True))
    row = _index(mongo, conn, "w_1")
    assert (row["ttl"], row["unique"]) == (3600, True)

    ok(mongo.set_index_hidden(conn, DB, "idx", "w_1", True))
    assert _index(mongo, conn, "w_1")["hidden"] is True
    ok(_edit(mongo, conn, "w_1", _index(mongo, conn, "w_1"), hidden=False))
    assert _index(mongo, conn, "w_1")["hidden"] is False


def test_unique_conversion_with_duplicates_is_rolled_back(env):
    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.create_index(conn, DB, "idx", "{v: 1}", ""))
    result = _edit(mongo, conn, "v_1", _index(mongo, conn, "v_1"), unique=True)
    assert not result["ok"] and "share a key" in result["error"], result
    from services.mongo_pool import pool

    raw = next(i for i in pool.client(1, conn)[DB]["idx"].list_indexes() if i["name"] == "v_1")
    assert not raw.get("unique") and not raw.get("prepareUnique"), raw


def test_rebuilds(env):
    mongo, conn = env["mongo"], env["conn"]
    # A new name: the new index is built before the old one is dropped.
    plan = ok(_edit(mongo, conn, "v_1", _index(mongo, conn, "v_1"), new_name="by_v", dry_run=True))
    assert plan["strategy"] == "build-then-drop"
    ok(_edit(mongo, conn, "v_1", _index(mongo, conn, "v_1"), new_name="by_v", keys="{v: 1, w: 1}"))
    assert _index(mongo, conn, "v_1") is None
    assert _index(mongo, conn, "by_v")["keys"] == {"v": 1, "w": 1}

    # Same name, new keys: drop, then build.
    result = ok(_edit(mongo, conn, "by_v", _index(mongo, conn, "by_v"), keys="{v: -1}"))
    assert result["strategy"] == "drop-then-build"
    assert _index(mongo, conn, "by_v")["keys"] == {"v": -1}

    # A build that fails (duplicate v) puts the old index back.
    failed = _edit(mongo, conn, "by_v", _index(mongo, conn, "by_v"), keys="{v: 1}", unique=True)
    assert not failed["ok"] and "was restored" in failed["error"], failed
    assert _index(mongo, conn, "by_v")["keys"] == {"v": -1}

    # Removing a TTL cannot be done in place.
    plan = ok(_edit(mongo, conn, "w_1", _index(mongo, conn, "w_1"), ttl_seconds=0, dry_run=True))
    assert plan["strategy"] == "drop-then-build" and "TTL removed" in plan["changes"]

    assert not mongo.update_index(conn, DB, "idx", "_id_", "{_id: 1}")["ok"]
    assert not mongo.update_index(conn, DB, "idx", "nope", "{a: 1}")["ok"]


def test_unchanged_definitions_are_not_changes(env):
    """Text indexes and date filters must round-trip through the edit form."""
    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.create_index(conn, DB, "idx", "{title: 'text'}", "search"))
    ok(mongo.create_index(conn, DB, "idx", "{at: 1}", "recent",
                          partial='{at: {$gt: ISODate("2024-01-01T00:00:00Z")}}',
                          options='{collation: {locale: "en", strength: 2}}'))
    search = _index(mongo, conn, "search")
    assert search["keys"] == {"title": "text"} and search["options_text"] == ""
    assert _edit(mongo, conn, "search", search, dry_run=True)["strategy"] == "none"
    recent = _index(mongo, conn, "recent")
    assert '"$date"' in recent["partial_text"]
    unchanged = _edit(mongo, conn, "recent", recent, dry_run=True)
    assert unchanged["strategy"] == "none", (unchanged, recent)
    plan = ok(_edit(mongo, conn, "recent", recent, hidden=True, dry_run=True))
    assert plan["strategy"] == "in-place", plan

    refused = mongo.create_index(conn, DB, "idx", "{z: 1}", "", options="{storageEngine: {}}")
    assert not refused["ok"] and "not supported" in refused["error"]


# -- dump and restore jobs (phase 36) ------------------------------------------

def _wait(service, job_id, timeout=30.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = ok(service.status(job_id))
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_dump_job_reports_progress_and_produces_the_zip(env):
    from services import jobs
    from services.job_service import JobService

    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.insert(conn, DB, "bulk", "[" + ",".join(f"{{i: {i}}}" for i in range(1200)) + "]"))
    service = JobService(env["user"])
    started = ok(service.start_dump(conn, DB, "bulk"))
    status = _wait(service, started["job"])
    assert status["state"] == "done" and status["download"] is True
    item = status["items"][0]
    assert (item["label"], item["done"], item["total"], item["state"]) == (f"{DB}.bulk", 1200, 1200, "done")
    assert any("1,200 documents" in line for line in status["log"])
    job = jobs.get(started["job"], 1)
    with zipfile.ZipFile(job.path) as archive:
        assert f"{DB}/bulk.bson" in archive.namelist()
    assert not JobService({"user_id": 999}).status(started["job"])["ok"], "per user"


def test_restore_job_progress_and_cancel(env):
    import bson

    from services import jobs
    from services.mongo_pool import pool
    from services.transfer import restore_archive

    client = pool.client(1, env["conn"])
    target = f"{DB}_jobs"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("src/big.bson", b"".join(bson.encode({"_id": i}) for i in range(3500)))
    data = buffer.getvalue()
    try:
        job = jobs.start(1, "restore", "t",
                         lambda progress: restore_archive(client, io.BytesIO(data), "skip", target, progress))
        from services.job_service import JobService

        status = _wait(JobService(env["user"]), job.id)
        item = status["items"][0]
        assert item["state"] == "done" and item["done"] == item["total"] > 0
        assert "3,500 documents" in item["note"] or "3,500 inserted" in item["note"]
        assert status["result"]["collections"][0]["inserted"] == 3500

        # Cancelled before it starts: stops at the first batch, keeps what it did.
        client.drop_database(target)
        cancel_now = jobs.Progress(None)
        cancel_now.check = lambda: (_ for _ in ()).throw(jobs.Cancelled())
        with pytest.raises(jobs.Cancelled):
            restore_archive(client, io.BytesIO(data), "skip", target, cancel_now)
        assert client[target]["big"].count_documents({}) == 1000
    finally:
        client.drop_database(target)


def test_an_edited_document_keeps_its_int64s(env):
    """Relaxed EJSON used to turn a small Int64 into an int32 on save."""
    import json

    from bson.int64 import Int64

    from services.mongo_pool import pool

    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.insert(conn, DB, "longs", "{_id: 'x', n: NumberLong(5), i: 5}"))
    row = ok(mongo.find(conn, DB, "longs"))["docs"][0]
    doc = ok(mongo.get_document(conn, DB, "longs", row["id"]))["doc"]
    assert doc["n"] == {"$numberLong": "5"}
    doc["note"] = "edited"
    ok(mongo.replace(conn, DB, "longs", row["id"], json.dumps(doc)))
    stored = pool.client(1, conn)[DB]["longs"].find_one({"_id": "x"})
    assert type(stored["n"]) is Int64 and type(stored["i"]) is int and stored["note"] == "edited"


# ----------------------------------------------------------------------
# Phase 39: a backend with no optional capabilities, on the same server.
# Every fallback runs against real MongoDB; everything else is refused.
# ----------------------------------------------------------------------

LIMITED_DB = f"{DB}_limited"


@pytest.fixture(scope="module")
def limited(env):
    from services import backends
    from services.backends.mongodb import MongoBackend

    class LimitedMongo(MongoBackend):
        name = "mongodb-limited"
        label = "Limited"
        capabilities = frozenset()

    backends.register(LimitedMongo())
    host, port, *rest = SPEC.split(":", 3)
    saved = env["conns"].save(
        name="limited", backend="mongodb-limited", host=host, port=int(port),
        username=rest[0] if rest else "", password=rest[1] if len(rest) > 1 else "",
    )
    assert saved["ok"], saved
    ok(env["mongo"].insert(env["conn"], LIMITED_DB, "items",
                           "[{_id: 1, n: 1}, {_id: 2, n: 2}, {_id: 3, n: 2}]"))
    yield saved["id"]
    env["mongo"].drop_database(env["conn"], LIMITED_DB)
    with backends._lock:
        backends._registry.pop("mongodb-limited", None)


def test_the_list_carries_each_backends_capabilities(env, limited):
    from services.backends import CAPABILITIES

    rows = {row["id"]: row for row in env["conns"].list()}
    assert rows[env["conn"]]["capabilities"] == sorted(CAPABILITIES)
    assert rows[env["conn"]]["backend_label"] == "MongoDB"
    assert rows[limited]["capabilities"] == []
    assert rows[limited]["backend_label"] == "Limited"


def test_limited_fallbacks_still_work(env, limited):
    mongo = env["mongo"]
    assert any(row["name"] == LIMITED_DB for row in ok(mongo.databases(limited))["databases"])
    colls = ok(mongo.collections(limited, LIMITED_DB))["collections"]
    assert colls == [{"name": "items", "type": "collection", "system": False}]

    stats = ok(mongo.stats(limited, LIMITED_DB, "items"))
    assert stats["count"] == 3 and stats["size"] is None and stats["index_sizes"] == {}

    schema = ok(mongo.schema(limited, LIMITED_DB, "items", 2))
    assert schema["sampled"] == 2 and [row["path"] for row in schema["fields"]] == ["_id", "n"]

    found = ok(mongo.find(limited, LIMITED_DB, "items", "{n: 2}", "{_id: -1}"))
    assert [row["doc"]["_id"] for row in found["docs"]] == [3, 2] and found["total"] == 2
    assert ok(mongo.count(limited, LIMITED_DB, "items", "{n: 2}"))["total"] == 2
    grouped = ok(mongo.aggregate(limited, LIMITED_DB, "items", "[{$group: {_id: '$n'}}]"))
    assert len(grouped["docs"]) == 2
    assert ok(mongo.preview_write(limited, LIMITED_DB, "items", "{n: 2}"))["matched"] == 2

    # The dashboard without serverStatus: names and collection counts only.
    row = next(row for row in ok(mongo.database_stats(limited))["databases"]
               if row["name"] == LIMITED_DB)
    assert row["collections"] == 1 and row["objects"] is None and row["profile_level"] is None


def test_limited_refuses_what_it_cannot_do(env, limited):
    mongo = env["mongo"]

    def refused(result: dict) -> str:
        assert not result["ok"], result
        return result["error"]

    assert refused(mongo.explain(limited, LIMITED_DB, "items")) == \
        "Limited connections cannot explain queries"
    assert "rename" in refused(mongo.rename_collection(limited, LIMITED_DB, "items", "x"))
    assert refused(mongo.server_status(limited)) == "Limited connections cannot report server status"
    assert "profile" in refused(mongo.set_profiler(limited, LIMITED_DB, 2))
    assert "profile" in refused(mongo.profiler_entries(limited, LIMITED_DB))
    assert "kill" in refused(mongo.kill_op(limited, "1"))
    assert "profile" in refused(mongo.set_profile_filter(limited, LIMITED_DB, "{op: 'query'}"))
    assert "empty collections" in refused(mongo.create_collection(limited, LIMITED_DB, "x"))
    assert "empty databases" in refused(mongo.create_database(limited, f"{LIMITED_DB}2", "x"))
    assert "TTL" in refused(mongo.create_index(limited, LIMITED_DB, "items", "{n: 1}",
                                               ttl_seconds=60))
    assert "hide" in refused(mongo.create_index(limited, LIMITED_DB, "items", "{n: 1}",
                                                hidden=True))
    assert "hide" in refused(mongo.set_index_hidden(limited, LIMITED_DB, "items", "x", True))
    # Nothing of the above reached the server.
    names = [row["name"] for row in ok(mongo.collections(env["conn"], LIMITED_DB))["collections"]]
    assert names == ["items"]


def test_limited_index_changes_rebuild_instead_of_collmod(env, limited):
    mongo = env["mongo"]
    ok(mongo.create_index(limited, LIMITED_DB, "items", "{n: 1}", name="n_1"))
    plan = ok(mongo.update_index(limited, LIMITED_DB, "items", "n_1", "{n: 1}",
                                 sparse=False, unique=False, dry_run=True))
    assert plan["strategy"] == "none"
    # Unique on is collMod on MongoDB; without it, a rebuild. n repeats, so the
    # unique build fails and the old index must come back.
    plan = ok(mongo.update_index(limited, LIMITED_DB, "items", "n_1", "{n: 1}",
                                 unique=True, dry_run=True))
    assert plan["strategy"] == "drop-then-build" and plan["changes"] == ["unique on"]
    failed = mongo.update_index(limited, LIMITED_DB, "items", "n_1", "{n: 1}", unique=True)
    assert not failed["ok"] and "restored" in failed["error"]
    names = [row["name"] for row in ok(mongo.indexes(limited, LIMITED_DB, "items"))["indexes"]]
    assert "n_1" in names
    ok(mongo.drop_index(limited, LIMITED_DB, "items", "n_1"))


def test_limited_dump_and_restore_without_raw_bson_or_bulk_write(env, limited, tmp_path):
    """Phase 39, step 5: the decoded paths, against the real server."""
    from services.mongo_pool import pool
    from services.transfer import restore_archive, write_dump

    client = pool.client(1, limited)
    path = tmp_path / "limited.zip"
    summary = write_dump(client, LIMITED_DB, "", path, capabilities=frozenset())
    assert summary["collections"][0]["documents"] == 3
    target = f"{LIMITED_DB}_back"
    try:
        for mode, field, expected in (("skip", "inserted", 3), ("skip", "skipped", 3),
                                      ("merge", "replaced", 3), ("drop", "inserted", 3)):
            with open(path, "rb") as archive:
                outcome = restore_archive(client, archive, mode, target,
                                          capabilities=frozenset())["collections"][0]
            assert outcome[field] == expected, (mode, outcome)
        assert sorted(doc["_id"] for doc in client[target]["items"].find()) == [1, 2, 3]
    finally:
        client.drop_database(target)


@pytest.fixture()
def tiny(env, tmp_path, monkeypatch):
    """A tinymongo store next to the real server, for copies both ways."""
    from services import backends
    from services.backends.tinymongo import TinyMongoBackend

    monkeypatch.setenv("MONGUANA_TINYMONGO_ROOT", str(tmp_path))
    backends.register(TinyMongoBackend())
    saved = env["conns"].save(name="tiny", backend="tinymongo",
                              options={"engine": "sqlite", "folder": ""})
    assert saved["ok"], saved
    yield saved["id"]
    from services.mongo_pool import pool

    pool.close(1, saved["id"])


def test_copy_mongodb_to_tinymongo_and_back(env, tiny):
    import time

    from services import jobs
    from services.job_service import JobService
    from services.mongo_pool import pool

    mongo, conn = env["mongo"], env["conn"]
    source_db, back_db = f"{DB}_copysrc", f"{DB}_copyback"
    ok(mongo.insert(conn, source_db, "things", """[
        {_id: ObjectId("65a1b2c3d4e5f60718293a4b"), when: ISODate("2026-01-01T00:00:00Z"),
         price: NumberDecimal("9.99"), key: UUID("12345678-1234-5678-1234-567812345678"),
         big: NumberLong("9007199254740993"), small: NumberLong("5"),
         nested: {list: [1, {x: "y"}]}},
        {_id: 2, name: "plain"},
    ]"""))
    ok(mongo.create_index(conn, source_db, "things", "{name: 1}", name="name_1"))
    service = JobService(env["user"])

    def run(started: dict) -> dict:
        ok(started)
        for _ in range(200):
            snapshot = jobs.get(started["job"], 1).snapshot(0)
            if snapshot["state"] != "running":
                return snapshot
            time.sleep(0.05)
        raise AssertionError("copy did not finish")

    try:
        assert run(service.start_copy(conn, source_db, tiny, "shop"))["state"] == "done"
        there = pool.client(1, tiny)["shop"]["things"]
        assert there.count_documents({}) == 2
        assert "name_1" in {index["name"] for index in there.list_indexes()}

        assert run(service.start_copy(tiny, "shop", conn, back_db))["state"] == "done"
        client = pool.client(1, conn)
        original = {doc["_id"]: doc for doc in client[source_db]["things"].find()}
        returned = {doc["_id"]: doc for doc in client[back_db]["things"].find()}
        assert returned == original
        # Equal as numbers; the stored BSON types, which == does not compare.
        # tinymongo hands Int64 back as int, so a small one returns as Int32:
        # the one type a trip through tinymongo loses (INSTALL.md says so).
        types = client[back_db]["things"].aggregate([
            {"$match": {"_id": {"$ne": 2}}},
            {"$project": {"_id": 0, **{field: {"$type": f"${field}"} for field in
                                       ("big", "small", "price", "when", "key")}}},
        ]).next()
        assert types == {"big": "long", "small": "int", "price": "decimal",
                         "when": "date", "key": "binData"}
        assert "name_1" in {index["name"] for index in client[back_db]["things"].list_indexes()}
    finally:
        client = pool.client(1, conn)
        client.drop_database(source_db)
        client.drop_database(back_db)


def test_server_status_reports_counters_and_operations(env):
    status = ok(env["mongo"].server_status(env["conn"]))
    assert status["status_error"] == "" and status["ops_error"] == ""
    server = status["status"]
    assert server["version"] and server["uptime_ms"] > 0
    assert server["connections"]["current"] >= 1
    assert set(server["opcounters"]) == {"insert", "query", "update", "delete", "getmore", "command"}
    assert server["mem"]["resident"]  # not dropped, as excluding "metrics" does
    # Its own $currentOp and the server's background threads are left out.
    assert not any("$currentOp" in op["query"] for op in status["ops"])
    assert not any(op["op"] == "none" and not op["client"] for op in status["ops"])


def test_database_stats_include_the_profiler_level(env):
    mongo = env["mongo"]
    ok(mongo.insert(env["conn"], DB, "stats_probe", "{_id: 1}"))
    row = next(row for row in ok(mongo.database_stats(env["conn"]))["databases"]
               if row["name"] == DB)
    assert row["objects"] >= 1 and row["collections"] >= 1 and row["profile_level"] == 0


def test_the_profiler_records_groups_reopens_and_clears(env):
    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.insert(env["conn"], DB, "profiled", "[{_id: 1, n: 1}, {_id: 2, n: 2}, {_id: 3, n: 2}]"))
    before = ok(mongo.profiler_status(conn, DB))
    try:
        # Level only: slowms is the server's and is left alone.
        settings = ok(mongo.set_profiler(conn, DB, 2))
        assert settings["level"] == 2 and settings["slowms"] == before["slowms"]
        # No index on n: a collection scan and a sort in memory.
        ok(mongo.find(conn, DB, "profiled", "{n: 2}", "{n: -1}"))
        ok(mongo.find(conn, DB, "profiled", "{n: 1}", "{n: -1}"))
        ok(mongo.aggregate(conn, DB, "profiled", "[{$match: {n: 2}}]"))

        entries = ok(mongo.profiler_entries(conn, DB, 50, 0, "query", "profiled"))["entries"]
        found = [entry for entry in entries if entry["open"] and entry["open"]["mode"] == "find"]
        assert found and all(entry["ns"] == f"{DB}.profiled" for entry in entries)
        reopen = found[0]["open"]
        assert reopen["coll"] == "profiled" and "\n" not in reopen["filter"]
        # What a tab gets back parses and runs.
        assert ok(mongo.find(conn, DB, "profiled", reopen["filter"], reopen["sort"]))["docs"]
        assert found[0]["collscan"] and found[0]["plan"] == "COLLSCAN"
        assert found[0]["in_memory_sort"]

        groups = ok(mongo.profiler_summary(conn, DB, 0, "query", "profiled"))["groups"]
        # {n: 2}, {n: 1} and the re-run above are one shape.
        assert len(groups) == 1 and groups[0]["count"] == 3

        aggregates = ok(mongo.profiler_entries(conn, DB, 50, 0, "command", "profiled"))["entries"]
        assert any((entry["open"] or {}).get("mode") == "aggregate" for entry in aggregates)

        # Reading the profile is itself profiled; that is left out.
        everything = ok(mongo.profiler_entries(conn, DB, 500))["entries"]
        assert everything and all(entry["ns"] != f"{DB}.system.profile" for entry in everything)
        assert not ok(mongo.profiler_entries(conn, DB, 50, 10_000_000))["entries"]
        assert not mongo.profiler_entries(conn, DB, 50, 0, "drop")["ok"]

        cleared = ok(mongo.profiler_clear(conn, DB))
        assert cleared["level"] == 2  # put back after the drop
        assert not ok(mongo.profiler_entries(conn, DB, 50, 0, "query"))["entries"]
    finally:
        ok(mongo.set_profiler(conn, DB, before["level"], before["slowms"], before["sample_rate"]))
    assert not mongo.set_profiler(conn, DB, 3)["ok"]
    assert not mongo.set_profiler(conn, "local", 1)["ok"]


def test_kill_op_interrupts_a_running_query(env):
    """A slow $where on a second client, found in the dashboard's list and killed."""
    import threading
    import time

    from pymongo import MongoClient
    from pymongo.errors import OperationFailure

    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.insert(conn, DB, "slow", "[" + ", ".join(f"{{_id: {i}}}" for i in range(40)) + "]"))
    host, port, *rest = SPEC.split(":", 3)
    other = MongoClient(host, int(port), username=rest[0] if rest else None,
                        password=rest[1] if len(rest) > 1 else None, appname="kill-me")
    outcome: dict = {}

    def slow() -> None:
        try:
            # ~20 s unless interrupted: 40 documents x 500 ms.
            list(other[DB]["slow"].find({"$where": "sleep(500) || true"}))
            outcome["result"] = "finished"
        except OperationFailure as exc:
            outcome["result"] = exc.code

    worker = threading.Thread(target=slow)
    worker.start()
    try:
        target = None
        for _ in range(50):
            ops = ok(mongo.server_status(conn))["ops"]
            # Not the driver's own monitor (an awaitable hello), which is hidden.
            assert not any(op["app"] == "kill-me" and op["op"] == "command" for op in ops)
            target = next((op for op in ops if op["app"] == "kill-me"), None)
            if target:
                break
            time.sleep(0.1)
        assert target, "the slow query never showed up in the list"
        assert ok(mongo.kill_op(conn, target["opid"]))["opid"] == target["opid"]
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert outcome["result"] == 11601  # Interrupted
    finally:
        worker.join(timeout=30)
        other.close()
    assert not mongo.kill_op(conn, "")["ok"]


def test_the_profile_filter_is_set_used_and_removed(env):
    mongo, conn = env["mongo"], env["conn"]
    ok(mongo.insert(conn, DB, "filtered", "[{_id: 1, n: 1}, {_id: 2, n: 2}]"))
    before = ok(mongo.profiler_status(conn, DB))
    try:
        # Shell syntax, outer braces optional; the level is not touched.
        settings = ok(mongo.set_profile_filter(conn, DB, "op: 'query', ns: /filtered$/"))
        assert settings["level"] == before["level"] and settings["filter"]
        ok(mongo.set_profiler(conn, DB, 1))
        ok(mongo.find(conn, DB, "filtered", "{n: 1}"))
        ok(mongo.count(conn, DB, "filtered", "{n: 2}"))  # a command: not recorded
        ops = {entry["op"] for entry in ok(mongo.profiler_entries(conn, DB, 50))["entries"]}
        assert ops == {"query"}

        refused = mongo.set_profile_filter(conn, DB, "{$where: 'true'}")
        assert not refused["ok"] and "$where" in refused["error"]
        assert not mongo.set_profile_filter(conn, DB, "{op: ")["ok"]
        assert not mongo.set_profile_filter(conn, DB, "{millis: {$bogus: 1}}")["ok"]
        assert not mongo.set_profile_filter(conn, "local", "{op: 'query'}")["ok"]

        assert ok(mongo.set_profile_filter(conn, DB, "  "))["filter"] is None
    finally:
        mongo.set_profile_filter(conn, DB, "")
        ok(mongo.set_profiler(conn, DB, before["level"]))
        ok(mongo.profiler_clear(conn, DB))
