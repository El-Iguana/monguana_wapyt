"""
BFF: small per-user UI state, kept on the server so it follows the person
across browsers (the original kept column layouts in localStorage).

Values are opaque JSON owned by the UI. Scoped by ``user_id`` like everything
else; bounded in size and count so a runaway client cannot fill the database.
"""
from __future__ import annotations

import json

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import get_db

MAX_KEY_LENGTH = 300
MAX_VALUE_BYTES = 32 * 1024
# Oldest entries beyond this are dropped: one per collection ever opened.
MAX_KEYS_PER_USER = 5000


@backend_for_frontend
@bff_policy(application="monguana")
class UiStateService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    def get(self, key: str) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            row = conn.execute(
                "SELECT payload FROM ui_state WHERE user_id = ? AND key = ?",
                (self._user_id, str(key)),
            ).fetchone()
        return {"ok": True, "value": json.loads(row["payload"]) if row else None}

    def clear(self, key: str) -> dict:
        """
        Forget ``key``. Its own method rather than ``set(key, None)``:
        pytincture validates arguments against the annotation, and ``None``
        for a ``dict`` parameter is a 400 before the call is made.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            conn.execute("DELETE FROM ui_state WHERE user_id = ? AND key = ?",
                         (self._user_id, str(key)))
        return {"ok": True}

    def set(self, key: str, value: dict) -> dict:
        """Store ``value`` under ``key``."""
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        key = str(key or "")
        if not key or len(key) > MAX_KEY_LENGTH:
            return {"ok": False, "error": "Bad key"}
        if not isinstance(value, dict):
            return {"ok": False, "error": "The value must be an object"}
        with get_db() as conn:
            payload = json.dumps(value, separators=(",", ":"))
            if len(payload.encode()) > MAX_VALUE_BYTES:
                return {"ok": False, "error": "Too large"}
            conn.execute(
                "INSERT INTO ui_state (user_id, key, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET payload = excluded.payload, "
                "updated_at = datetime('now')",
                (self._user_id, key, payload),
            )
            conn.execute(
                "DELETE FROM ui_state WHERE user_id = ? AND key NOT IN ("
                "  SELECT key FROM ui_state WHERE user_id = ? "
                "  ORDER BY updated_at DESC, rowid DESC LIMIT ?)",
                (self._user_id, self._user_id, MAX_KEYS_PER_USER),
            )
        return {"ok": True}
