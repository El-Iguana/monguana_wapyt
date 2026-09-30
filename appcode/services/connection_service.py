"""
BFF: saved connection profiles — MongoDB servers, or another backend's stores
(``services.backends``).

Every query is scoped by the caller's own ``user_id``. Secrets never travel
back to the browser: ``list``/``get`` return ``has_password`` / ``has_uri``
flags, and :meth:`save` overwrites a stored secret only when a replacement is
supplied — so "leave unchanged" and "clear it" are different requests.
"""
from __future__ import annotations

from typing import Any, Optional

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import encode_options, encrypt, fetch_connection, get_db

_PUBLIC_COLUMNS = (
    "id, name, backend, folder, host, port, username, auth_source, tls, direct, "
    "default_db, notes, created_at"
)

_SENTINEL_UNCHANGED = "\x00unchanged\x00"


@backend_for_frontend
@bff_policy(application="monguana")
class ConnectionService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list(self) -> list:
        """Every profile the caller owns, ordered for the sidebar tree."""
        if not self._user_id:
            return []
        from services.mongo_pool import pool

        open_ids = set(pool.open_ids(self._user_id))
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT {_PUBLIC_COLUMNS}, "
                "  (password <> '') AS has_password, (uri <> '') AS has_uri "
                "FROM connections WHERE user_id = ? "
                "ORDER BY folder COLLATE NOCASE, name COLLATE NOCASE",
                (self._user_id,),
            ).fetchall()
        return [self._public(row, open_ids) for row in rows]

    def get(self, conn_id: int) -> dict:
        if not self._user_id:
            return {}
        with get_db() as conn:
            row = conn.execute(
                f"SELECT {_PUBLIC_COLUMNS}, "
                "  (password <> '') AS has_password, (uri <> '') AS has_uri "
                "FROM connections WHERE id = ? AND user_id = ?",
                (int(conn_id), self._user_id),
            ).fetchone()
        return self._public(row, set()) if row else {}

    def folders(self) -> list:
        if not self._user_id:
            return []
        with get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT folder FROM connections "
                "WHERE user_id = ? AND folder <> '' ORDER BY folder COLLATE NOCASE",
                (self._user_id,),
            ).fetchall()
        return [row["folder"] for row in rows]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def save(
        self,
        conn_id: Optional[int] = None,
        name: str = "",
        folder: str = "",
        host: str = "",
        port: int = 27017,
        username: str = "",
        auth_source: str = "admin",
        tls: bool = False,
        direct: bool = True,
        default_db: str = "",
        notes: str = "",
        password: str = _SENTINEL_UNCHANGED,
        uri: str = _SENTINEL_UNCHANGED,
        backend: str = "mongodb",
        options: Optional[dict] = None,
    ) -> dict:
        """
        Create or update a profile.

        ``password`` and ``uri`` are three-state: omitted leaves the stored
        value alone, ``""`` clears it, anything else replaces it. ``options``
        (the backend's own settings) is kept when omitted.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}

        existing: dict = {}
        if conn_id and (uri == _SENTINEL_UNCHANGED or options is None):
            existing = fetch_connection(int(conn_id), self._user_id) or {}
        profile = {
            "backend": backend,
            "host": host,
            "port": port,
            "uri": existing.get("uri", "") if uri == _SENTINEL_UNCHANGED else (uri or ""),
            "options": existing.get("options", {}) if options is None else options,
        }
        errors = self._validate(name, profile)
        if errors:
            return {"ok": False, "errors": errors}

        values: dict[str, Any] = {
            "name": name.strip(),
            "backend": (backend or "").strip() or "mongodb",
            "folder": (folder or "").strip(),
            "host": (host or "").strip(),
            "port": int(port or 27017),
            "username": (username or "").strip(),
            "auth_source": (auth_source or "").strip() or "admin",
            "tls": int(bool(tls)),
            "direct": int(bool(direct)),
            "default_db": (default_db or "").strip(),
            "notes": notes or "",
        }
        if password != _SENTINEL_UNCHANGED:
            values["password"] = encrypt(password or "")
        if uri != _SENTINEL_UNCHANGED:
            values["uri"] = encrypt((uri or "").strip())
        if options is not None:
            values["options"] = encode_options(options)

        with get_db() as conn:
            if conn_id:
                owned = conn.execute(
                    "SELECT id FROM connections WHERE id = ? AND user_id = ?",
                    (int(conn_id), self._user_id),
                ).fetchone()
                if owned is None:
                    return {"ok": False, "error": "Connection not found"}
                assignments = ", ".join(f"{column} = ?" for column in values)
                conn.execute(
                    f"UPDATE connections SET {assignments} WHERE id = ? AND user_id = ?",
                    [*values.values(), int(conn_id), self._user_id],
                )
                new_id = int(conn_id)
            else:
                values.setdefault("password", "")
                values.setdefault("uri", "")
                values.setdefault("options", "")
                columns = ", ".join(["user_id", *values])
                marks = ", ".join("?" * (len(values) + 1))
                cursor = conn.execute(
                    f"INSERT INTO connections ({columns}) VALUES ({marks})",
                    [self._user_id, *values.values()],
                )
                new_id = cursor.lastrowid

        # The pool re-reads the profile on every call and notices a changed
        # fingerprint anyway; closing here just frees the old sockets now.
        from services.mongo_pool import pool

        pool.close(self._user_id, new_id)
        return {"ok": True, "id": new_id}

    def delete(self, conn_id: int) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            cursor = conn.execute(
                "DELETE FROM connections WHERE id = ? AND user_id = ?",
                (int(conn_id), self._user_id),
            )
        if cursor.rowcount == 0:
            return {"ok": False, "error": "Connection not found"}
        from services.mongo_pool import pool

        pool.close(self._user_id, int(conn_id))
        return {"ok": True}

    def duplicate(self, conn_id: int) -> dict:
        """A copy of a profile, secrets included, named "… (copy)"."""
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO connections (user_id, name, backend, options, folder, host, "
                "  port, username, password, auth_source, tls, direct, uri, default_db, notes) "
                "SELECT user_id, name || ' (copy)', backend, options, folder, host, "
                "  port, username, password, auth_source, tls, direct, uri, default_db, notes "
                "FROM connections WHERE id = ? AND user_id = ?",
                (int(conn_id), self._user_id),
            )
        if cursor.rowcount == 0:
            return {"ok": False, "error": "Connection not found"}
        return {"ok": True, "id": cursor.lastrowid}

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def test(
        self,
        conn_id: Optional[int] = None,
        host: str = "",
        port: int = 27017,
        username: str = "",
        auth_source: str = "admin",
        tls: bool = False,
        direct: bool = True,
        password: str = _SENTINEL_UNCHANGED,
        uri: str = _SENTINEL_UNCHANGED,
        backend: str = "mongodb",
        options: Optional[dict] = None,
    ) -> dict:
        """
        Ping with the editor's current values, saved or not.

        For a stored profile the omitted secrets fall back to the stored ones,
        so testing an edit does not mean retyping the password.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        stored: dict = {}
        if conn_id:
            stored = fetch_connection(int(conn_id), self._user_id) or {}
            if not stored:
                return {"ok": False, "error": "Connection not found"}
        profile = {
            "backend": backend,
            "options": stored.get("options", {}) if options is None else options,
            "host": host,
            "port": port,
            "username": username,
            "auth_source": auth_source,
            "tls": tls,
            "direct": direct,
            "password": stored.get("password", "") if password == _SENTINEL_UNCHANGED else password,
            "uri": stored.get("uri", "") if uri == _SENTINEL_UNCHANGED else uri,
        }
        errors = self._validate("x", profile)
        if errors:
            return {"ok": False, "error": next(iter(errors.values()))}

        from services import backends

        chosen = backends.for_profile(profile)
        return chosen.test(chosen.connect_options(profile))

    def ping(self, conn_id: int) -> dict:
        """Ping a saved profile through the pool — this is "Connect"."""
        from services.mongo_pool import ProfileNotFound, error_text, pool

        try:
            client = pool.client(self._user_id, int(conn_id))
            client.admin.command("ping")
            return {"ok": True}
        except ProfileNotFound:
            return {"ok": False, "error": "Connection not found"}
        except Exception as exc:  # noqa: BLE001 - shown to the person
            pool.close(self._user_id, int(conn_id))
            return {"ok": False, "error": error_text(exc)}

    def disconnect(self, conn_id: int) -> dict:
        from services.mongo_pool import pool

        pool.close(self._user_id, int(conn_id))
        return {"ok": True}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _public(row: Any, open_ids: set) -> dict:
        from services import backends

        data = dict(row)
        # What the UI may offer for this profile (see backends.CAPABILITIES).
        try:
            backend = backends.get(data.get("backend") or "")
            data["backend_label"] = backend.label
            data["capabilities"] = sorted(backend.capabilities)
        except backends.BackendUnavailable:
            data["backend_label"] = data.get("backend") or ""
            data["capabilities"] = []
        data["tls"] = bool(data.get("tls"))
        data["direct"] = bool(data.get("direct"))
        data["has_password"] = bool(data.pop("has_password", 0))
        data["has_uri"] = bool(data.pop("has_uri", 0))
        data["open"] = data["id"] in open_ids
        return data

    @staticmethod
    def _validate(name: str, profile: dict) -> dict:
        """The name here; everything else belongs to the profile's backend."""
        from services import backends

        errors: dict = {}
        if not (name or "").strip():
            errors["name"] = "Name is required"
        try:
            chosen = backends.for_profile(profile)
        except backends.BackendUnavailable as exc:
            errors["backend"] = str(exc)
            return errors
        errors.update(chosen.validate(profile))
        return errors
