"""
The tinymongo backend (phase 39, step 3), through the real services, on both
engines. No MongoDB needed: every store is a temporary folder.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("tinymongo")

ENGINES = ("sqlite", "json")


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """A tinymongo root, and a registry that has re-read the environment."""
    from services import backends

    folder = tmp_path / "tiny"
    (folder / "stores").mkdir(parents=True)
    monkeypatch.setenv("MONGUANA_TINYMONGO_ROOT", str(folder))
    with backends._lock:
        saved = dict(backends._registry), backends._builtins_loaded
        backends._registry.clear()
        backends._builtins_loaded = False
    yield folder
    with backends._lock:
        backends._registry.clear()
        backends._registry.update(saved[0])
        backends._builtins_loaded = saved[1]


@pytest.fixture()
def data(tmp_path):
    from services import db

    saved = (db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet)
    home = tmp_path / "data"
    db.DATA_DIR, db.DB_PATH = home, home / "monguana.db"
    db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = home / "k", home / "s", None
    db.init_db()
    yield db
    from services.mongo_pool import pool

    pool.close_all()
    db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = saved


@pytest.fixture()
def services(root, data):
    from services.connection_service import ConnectionService
    from services.mongo_service import MongoService

    user = {"user_id": 1}
    return ConnectionService(user), MongoService(user)


def ok(result: dict) -> dict:
    assert result.get("ok"), result
    return result


def refused(result: dict) -> str:
    assert not result.get("ok"), result
    return result.get("error") or next(iter(result.get("errors", {}).values()))


# ----------------------------------------------------------------------
# Switched on by the root, and confined to it
# ----------------------------------------------------------------------


def test_off_without_a_root(monkeypatch):
    from services.backends import tinymongo

    monkeypatch.delenv("MONGUANA_TINYMONGO_ROOT", raising=False)
    assert not tinymongo.available()


def test_registered_with_a_root(services):
    conns, _ = services
    names = [row["name"] for row in conns.backends()]
    assert names == ["mongodb", "tinymongo"]
    tiny = conns.backends()[1]
    assert [field["id"] for field in tiny["fields"]] == ["engine", "folder"]
    assert conns.backends()[0]["fields"] is None


@pytest.mark.parametrize("folder, message", [
    ("../", "outside"),
    ("stores/../../", "outside"),
    ("/etc", "relative"),
    ("C:/Windows", "relative"),
    ("missing", "does not exist"),
])
def test_folders_outside_the_root_are_refused(services, folder, message):
    conns, _ = services
    result = conns.save(name="x", backend="tinymongo",
                        options={"engine": "sqlite", "folder": folder})
    assert message in result["errors"]["folder"]


def test_a_symlink_out_of_the_root_is_refused(services, root, tmp_path):
    conns, _ = services
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, root / "escape")
    result = conns.save(name="x", backend="tinymongo",
                        options={"engine": "sqlite", "folder": "escape"})
    assert "outside" in result["errors"]["folder"]


def test_a_folder_swapped_for_a_symlink_after_saving_is_refused(services, root, tmp_path):
    conns, mongo = services
    (root / "later").mkdir()
    saved = ok(conns.save(name="x", backend="tinymongo",
                          options={"engine": "sqlite", "folder": "later"}))
    (root / "later").rmdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, root / "later")
    assert "outside" in refused(mongo.databases(saved["id"]))


def test_unknown_engines_are_refused(services):
    conns, _ = services
    result = conns.save(name="x", backend="tinymongo", options={"engine": "memory", "folder": ""})
    assert "engine" in result["errors"]


# ----------------------------------------------------------------------
# A store, end to end
# ----------------------------------------------------------------------


@pytest.fixture(params=ENGINES)
def store(request, services, root):
    conns, mongo = services
    saved = ok(conns.save(name=f"tiny-{request.param}", backend="tinymongo",
                          options={"engine": request.param, "folder": "stores"}))
    return {"conn": saved["id"], "conns": conns, "mongo": mongo,
            "engine": request.param, "path": root / "stores"}


def test_the_profile_as_the_browser_sees_it(store):
    row = next(row for row in store["conns"].list() if row["id"] == store["conn"])
    assert row["backend"] == "tinymongo" and row["backend_label"] == "tinymongo"
    assert row["options"] == {"engine": store["engine"], "folder": "stores"}
    assert row["capabilities"] == ["authorized_listing", "create_collection"]


def test_the_test_button_creates_nothing(store):
    result = ok(store["conns"].test(conn_id=store["conn"], backend="tinymongo"))
    assert result["version"].startswith("tinymongo") and "0 database(s)" in result["version"]
    assert [p.name for p in store["path"].iterdir() if not p.name.startswith(".")] == []


def test_databases_collections_and_documents(store):
    mongo, conn = store["mongo"], store["conn"]
    ok(mongo.create_database(conn, "shop", "orders"))
    assert [row["name"] for row in ok(mongo.databases(conn))["databases"]] == ["shop"]
    ok(mongo.create_collection(conn, "shop", "customers"))
    colls = ok(mongo.collections(conn, "shop"))["collections"]
    assert sorted(row["name"] for row in colls) == ["customers", "orders"]
    assert ok(mongo.stats(conn, "shop", "customers"))["count"] == 0

    inserted = ok(mongo.insert(conn, "shop", "orders", """[
        {number: 1, status: "paid", total: NumberDecimal("10.50"), placed: ISODate("2026-01-01T00:00:00Z")},
        {number: 2, status: "new", total: NumberDecimal("3.25"), placed: ISODate("2026-01-02T00:00:00Z")},
        {number: 3, status: "paid", ref: ObjectId("65a1b2c3d4e5f60718293a4b"), key: UUID("12345678-1234-5678-1234-567812345678")},
    ]"""))
    assert inserted["count"] == 3

    found = ok(mongo.find(conn, "shop", "orders", "{status: 'paid'}", "{number: -1}", "{number: 1}"))
    assert [row["doc"]["number"] for row in found["docs"]] == [3, 1]
    assert found["total"] == 2 and found["exact"]
    assert set(found["docs"][0]["doc"]) == {"_id", "number"}
    dated = ok(mongo.find(conn, "shop", "orders", '{placed: {$gte: ISODate("2026-01-02")}}'))
    assert [row["doc"]["number"] for row in dated["docs"]] == [2]
    paged = ok(mongo.find(conn, "shop", "orders", "", "{number: 1}", "", page=2, page_size=2))
    assert [row["doc"]["number"] for row in paged["docs"]] == [3]
    assert ok(mongo.count(conn, "shop", "orders", "{status: 'paid'}"))["total"] == 2

    by_ref = ok(mongo.find(conn, "shop", "orders", '{ref: ObjectId("65a1b2c3d4e5f60718293a4b")}'))
    row = by_ref["docs"][0]
    full = ok(mongo.get_document(conn, "shop", "orders", row["id"]))["doc"]
    assert full["key"] == {"$uuid": "12345678-1234-5678-1234-567812345678"}
    ok(mongo.replace(conn, "shop", "orders", row["id"], '{number: 30, status: "shipped"}'))
    assert ok(mongo.get_document(conn, "shop", "orders", row["id"]))["doc"]["number"] == 30

    assert ok(mongo.preview_write(conn, "shop", "orders", "{status: 'paid'}"))["matched"] == 1
    assert ok(mongo.update_where(conn, "shop", "orders", "{status: 'new'}",
                                 "{$set: {status: 'paid'}}", multi=True))["modified"] == 1
    grouped = ok(mongo.aggregate(conn, "shop", "orders",
                                 "[{$group: {_id: '$status', n: {$sum: 1}}}, {$sort: {_id: 1}}]"))
    assert [doc["doc"] for doc in grouped["docs"]] == [
        {"_id": "paid", "n": 2}, {"_id": "shipped", "n": 1}]
    ok(mongo.delete_document(conn, "shop", "orders", row["id"]))
    assert ok(mongo.delete_where(conn, "shop", "orders", "{number: 1}"))["deleted"] == 1

    stats = ok(mongo.stats(conn, "shop", "orders"))
    assert stats["count"] == 1 and stats["size"] is None
    schema = ok(mongo.schema(conn, "shop", "orders"))
    assert "status" in [field["path"] for field in schema["fields"]]

    ok(mongo.drop_collection(conn, "shop", "customers"))
    assert [row["name"] for row in ok(mongo.collections(conn, "shop"))["collections"]] == ["orders"]
    ok(mongo.drop_database(conn, "shop"))
    assert ok(mongo.databases(conn))["databases"] == []


def test_indexes(store):
    mongo, conn = store["mongo"], store["conn"]
    ok(mongo.insert(conn, "shop", "orders", "[{n: 1, s: 'a'}, {n: 1, s: 'b'}]"))
    ok(mongo.create_index(conn, "shop", "orders", "{s: 1}", name="s_1", unique=True, sparse=True))
    ok(mongo.create_index(conn, "shop", "orders", "{n: 1}", name="n_1"))
    listed = {row["name"]: row for row in ok(mongo.indexes(conn, "shop", "orders"))["indexes"]}
    assert set(listed) == {"_id_", "s_1", "n_1"}
    assert listed["s_1"]["unique"] and listed["s_1"]["sparse"]

    # No collMod: a change is a rebuild; a unique index on repeats fails and
    # the old one comes back.
    plan = ok(mongo.update_index(conn, "shop", "orders", "n_1", "{n: 1}", unique=True, dry_run=True))
    assert plan["strategy"] == "drop-then-build"
    assert "restored" in refused(mongo.update_index(conn, "shop", "orders", "n_1", "{n: 1}",
                                                    unique=True))
    assert "n_1" in {row["name"] for row in ok(mongo.indexes(conn, "shop", "orders"))["indexes"]}
    ok(mongo.update_index(conn, "shop", "orders", "n_1", "{n: 1, s: 1}", new_name="n_s"))
    names = {row["name"] for row in ok(mongo.indexes(conn, "shop", "orders"))["indexes"]}
    assert names == {"_id_", "s_1", "n_s"}
    ok(mongo.drop_index(conn, "shop", "orders", "n_s"))
    # tinymongo's own limit, shown as its text.
    assert "ascending" in refused(mongo.create_index(conn, "shop", "orders", "{n: -1}"))


def test_what_tinymongo_cannot_do_is_refused_with_a_reason(store):
    mongo, conn = store["mongo"], store["conn"]
    ok(mongo.insert(conn, "shop", "orders", "{n: 1}"))
    assert refused(mongo.explain(conn, "shop", "orders")) == \
        "tinymongo connections cannot explain queries"
    assert "rename" in refused(mongo.rename_collection(conn, "shop", "orders", "x"))
    assert "capped" in refused(mongo.create_collection(conn, "shop", "c", capped=True, size=100))
    assert "TTL" in refused(mongo.create_index(conn, "shop", "orders", "{n: 1}", ttl_seconds=5))
    # tinymongo's own refusals come through as text.
    assert "$sample" in refused(mongo.aggregate(conn, "shop", "orders", "[{$sample: {size: 1}}]"))

    from services.job_service import JobService

    assert "cannot be dumped" in refused(JobService({"user_id": 1}).start_dump(conn, "shop"))
