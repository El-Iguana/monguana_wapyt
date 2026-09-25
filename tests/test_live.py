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
