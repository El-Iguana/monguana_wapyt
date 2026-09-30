"""
The backend registry (phase 39, step 1): profiles name a backend, and the pool
and ConnectionService build, validate and test through it. None of this dials
a server: a fake backend stands in for one.
"""
from __future__ import annotations

import sqlite3

import pytest


class FakeClient:
    def __init__(self, options: dict) -> None:
        self.options = options
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    name = "fake"
    label = "Fake"

    def validate(self, profile: dict) -> dict:
        folder = (profile.get("options") or {}).get("folder", "")
        return {} if folder else {"folder": "Folder is required"}

    def connect_options(self, profile: dict) -> dict:
        return dict(profile.get("options") or {})

    def open(self, options: dict) -> FakeClient:
        return FakeClient(options)

    def test(self, options: dict) -> dict:
        return {"ok": True, "version": f"fake {options.get('folder')}", "ms": 0}


@pytest.fixture()
def fake():
    from services import backends

    backends.register(FakeBackend())
    yield
    with backends._lock:
        backends._registry.pop("fake", None)


@pytest.fixture()
def data(tmp_path):
    from services import db

    saved = (db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet)
    db.DATA_DIR, db.DB_PATH = tmp_path, tmp_path / "monguana.db"
    db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = tmp_path / "k", tmp_path / "s", None
    yield db
    from services.mongo_pool import pool

    pool.close_all()
    db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = saved


@pytest.fixture()
def conns(data, fake):
    data.init_db()
    from services.connection_service import ConnectionService

    return ConnectionService({"user_id": 1})


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


def test_mongodb_is_built_in_and_the_default():
    from services import backends

    assert "mongodb" in backends.names()
    assert backends.get("").name == "mongodb"
    assert backends.for_profile({}).name == "mongodb"
    assert backends.for_profile({"backend": "mongodb"}).label == "MongoDB"


def test_an_unknown_backend_is_reported_not_guessed():
    from services import backends

    with pytest.raises(backends.BackendUnavailable, match="'nosuch'"):
        backends.get("nosuch")


def test_registered_backends_are_listed(fake):
    from services import backends

    assert backends.names() == ["fake", "mongodb"]


# ----------------------------------------------------------------------
# The MongoDB backend: unchanged behaviour, moved
# ----------------------------------------------------------------------


def test_mongodb_validation():
    from services.backends.mongodb import MongoBackend

    check = MongoBackend().validate
    assert check({"host": "db", "port": 27017}) == {}
    assert check({"host": "", "port": 27017}) == {
        "host": "Host is required (or give a connection string)"}
    assert check({"host": "db", "port": 70000}) == {"port": "Port must be between 1 and 65535"}
    assert check({"uri": "mongodb+srv://x"}) == {}
    assert "uri" in check({"uri": "postgres://x"})


def test_mongodb_credentials_go_in_as_keywords():
    from services.backends.mongodb import client_options

    options = client_options({"host": "db", "port": 27018, "username": "root",
                              "password": "p@ss:w/rd"})
    assert options["host"] == "db" and options["port"] == 27018
    assert options["password"] == "p@ss:w/rd" and options["authSource"] == "admin"
    assert options["directConnection"] is True
    assert client_options({"uri": "mongodb://h"})["host"] == "mongodb://h"


# ----------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------


def test_profiles_saved_before_backends_become_mongodb(data):
    data.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data.DB_PATH) as conn:
        conn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, pw_hash TEXT,"
            "  is_admin INTEGER, created_at TEXT);"
            "INSERT INTO users VALUES (1, 'admin', 'x', 1, '');"
            "CREATE TABLE connections (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  user_id INTEGER, name TEXT, folder TEXT DEFAULT '', host TEXT DEFAULT '',"
            "  port INTEGER DEFAULT 27017, username TEXT DEFAULT '', password TEXT DEFAULT '',"
            "  auth_source TEXT DEFAULT 'admin', tls INTEGER DEFAULT 0, direct INTEGER DEFAULT 1,"
            "  uri TEXT DEFAULT '', default_db TEXT DEFAULT '', notes TEXT DEFAULT '',"
            "  created_at TEXT DEFAULT '');"
            "INSERT INTO connections (user_id, name, host) VALUES (1, 'old', 'db');"
        )
    data.init_db()
    profile = data.fetch_connection(1, 1)
    assert profile["backend"] == "mongodb"
    assert profile["options"] == {}


def test_options_are_stored_encrypted_and_kept_when_omitted(conns, data):
    saved = conns.save(name="files", backend="fake", options={"folder": "shop"})
    assert saved["ok"], saved
    with data.get_db() as conn:
        raw = conn.execute("SELECT backend, options FROM connections").fetchone()
    assert raw["backend"] == "fake"
    assert raw["options"].startswith("fernet:") and "shop" not in raw["options"]

    assert conns.save(conn_id=saved["id"], name="renamed", backend="fake")["ok"]
    profile = data.fetch_connection(saved["id"], 1)
    assert profile["name"] == "renamed"
    assert profile["options"] == {"folder": "shop"}


def test_the_backend_validates_its_own_fields(conns):
    result = conns.save(name="", backend="fake", options={})
    assert result["errors"] == {"name": "Name is required", "folder": "Folder is required"}
    assert conns.save(name="x", backend="nosuch")["errors"] == {
        "backend": "The 'nosuch' backend is not installed"}


def test_list_duplicate_and_mongodb_default(conns, data):
    mongo = conns.save(name="server", host="db", port=27017)
    files = conns.save(name="files", backend="fake", options={"folder": "shop"})
    assert mongo["ok"] and files["ok"]
    by_name = {row["name"]: row for row in conns.list()}
    assert by_name["server"]["backend"] == "mongodb"
    assert by_name["files"]["backend"] == "fake"
    assert "options" not in by_name["files"]

    copy = conns.duplicate(files["id"])
    profile = data.fetch_connection(copy["id"], 1)
    assert profile["name"] == "files (copy)"
    assert (profile["backend"], profile["options"]) == ("fake", {"folder": "shop"})


# ----------------------------------------------------------------------
# Test button and pool
# ----------------------------------------------------------------------


def test_the_test_button_uses_the_backend(conns):
    assert conns.test(backend="fake", options={"folder": "a"}) == {
        "ok": True, "version": "fake a", "ms": 0}
    assert conns.test(backend="fake", options={}) == {"ok": False, "error": "Folder is required"}
    saved = conns.save(name="files", backend="fake", options={"folder": "kept"})
    assert conns.test(conn_id=saved["id"], backend="fake")["version"] == "fake kept"


def test_the_pool_opens_through_the_backend_and_notices_edits(conns):
    from services.mongo_pool import pool

    saved = conns.save(name="files", backend="fake", options={"folder": "one"})
    first = pool.client(1, saved["id"])
    assert isinstance(first, FakeClient) and first.options == {"folder": "one"}
    assert pool.client(1, saved["id"]) is first

    # Saving closes the pooled client; the next call opens one for the edit.
    conns.save(conn_id=saved["id"], name="files", backend="fake", options={"folder": "two"})
    assert first.closed
    second = pool.client(1, saved["id"])
    assert second.options == {"folder": "two"}


def test_the_pool_rebuilds_on_a_changed_fingerprint(conns, data):
    from services.mongo_pool import pool

    saved = conns.save(name="files", backend="fake", options={"folder": "one"})
    first = pool.client(1, saved["id"])
    # Changed behind the service's back: only the fingerprint can notice.
    with data.get_db() as conn:
        conn.execute("UPDATE connections SET options = ? WHERE id = ?",
                     (data.encode_options({"folder": "three"}), saved["id"]))
    second = pool.client(1, saved["id"])
    assert second is not first and first.closed
    assert second.options == {"folder": "three"}


def test_a_profile_whose_backend_is_gone_says_so(conns, data):
    from services import backends
    from services.mongo_pool import pool

    saved = conns.save(name="files", backend="fake", options={"folder": "one"})
    with backends._lock:
        backends._registry.pop("fake")
    with pytest.raises(backends.BackendUnavailable):
        pool.client(1, saved["id"])
