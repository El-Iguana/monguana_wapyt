"""Per-user UI state (column layouts)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def service(tmp_path):
    from services import db

    saved = (db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet)
    db.DATA_DIR, db.DB_PATH = tmp_path, tmp_path / "monguana.db"
    db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = tmp_path / "k", tmp_path / "s", None
    db.init_db()
    with db.get_db() as conn:
        conn.execute("INSERT INTO users (username, pw_hash) VALUES ('bob', 'x')")
    from services.ui_state_service import UiStateService

    yield UiStateService
    db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = saved


def test_set_get_delete(service):
    alice = service({"user_id": 1})
    assert alice.get("columns:1:shop:orders") == {"ok": True, "value": None}
    layout = {"order": ["_id", "name"], "widths": {"name": 140}}
    assert alice.set("columns:1:shop:orders", layout)["ok"]
    assert alice.get("columns:1:shop:orders")["value"] == layout
    assert alice.clear("columns:1:shop:orders")["ok"]
    assert alice.get("columns:1:shop:orders")["value"] is None


def test_state_is_per_user(service):
    service({"user_id": 1}).set("k", {"a": 1})
    assert service({"user_id": 2}).get("k")["value"] is None
    assert service({}).get("k")["ok"] is False


def test_limits(service, monkeypatch):
    alice = service({"user_id": 1})
    assert not alice.set("k", {"x": "y" * 40_000})["ok"]
    assert not alice.set("", {"a": 1})["ok"]
    assert not alice.set("k", None)["ok"]
    import services.ui_state_service as module

    monkeypatch.setattr(module, "MAX_KEYS_PER_USER", 3)
    for index in range(5):
        alice.set(f"k{index}", {"i": index})
    kept = [key for key in (f"k{i}" for i in range(5)) if alice.get(key)["value"]]
    assert kept == ["k2", "k3", "k4"]
