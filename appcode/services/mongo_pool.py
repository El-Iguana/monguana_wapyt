"""
The MongoClient pool, and anything else that must outlive a single call.

**Not a BFF module, on purpose.** pytincture re-executes a BFF module's source
on every call, so a client cache defined in ``mongo_service.py`` would be a new,
empty dict per request: every click would dial a fresh connection pool and
nothing would ever close one. IguanaXterm lost a lot of time to exactly this
(see its CLAUDE.md). This module is imported normally and cached in
``sys.modules``, so ``pool`` is a real singleton; ``tests/test_bff_state.py``
proves it the way pytincture loads things.

A ``MongoClient`` is itself a thread-safe connection pool, so one per profile
is right: the BFF's worker threads share it.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Optional

from pymongo import MongoClient

from services.db import fetch_connection

APP_NAME = "Monguana"

# Close a client that has gone unused for this long. It holds sockets on the
# server (and a monitor thread here) for every profile anyone ever opened.
_IDLE_TIMEOUT_SECONDS = 15 * 60


class ProfileNotFound(LookupError):
    pass


def client_options(profile: dict) -> dict:
    """
    ``MongoClient`` keyword arguments for a saved profile.

    Credentials go in as keyword arguments rather than being spliced into a
    URI. The original built ``mongodb://user:password@host`` by string
    formatting, so a password containing ``@``, ``:`` or ``/`` produced a
    different URI altogether (MongoDB requires those percent-encoded).
    """
    options: dict = {
        "appname": APP_NAME,
        "serverSelectionTimeoutMS": 5000,
        "connectTimeoutMS": 5000,
        # Datetimes come back aware, so what the UI shows as UTC is UTC.
        "tz_aware": True,
        # Subtype-4 UUIDs, matching mql's JSON options; see there.
        "uuidRepresentation": "standard",
    }
    uri = (profile.get("uri") or "").strip()
    if uri:
        options["host"] = uri
        return options

    options["host"] = (profile.get("host") or "localhost").strip()
    options["port"] = int(profile.get("port") or 27017)
    username = profile.get("username") or ""
    if username:
        options["username"] = username
        options["password"] = profile.get("password") or ""
        options["authSource"] = profile.get("auth_source") or "admin"
    if profile.get("tls"):
        options["tls"] = True
    # A single host is a direct connection unless told otherwise; without it,
    # a replica-set member answers with its set's config and the driver tries
    # to reach hostnames that may only resolve inside the set's network.
    options["directConnection"] = bool(profile.get("direct", True))
    return options


def fingerprint(options: dict) -> str:
    """Changes whenever anything that affects the connection changes."""
    material = repr(sorted(options.items())).encode()
    return hashlib.sha256(material).hexdigest()


class _Entry:
    __slots__ = ("client", "fingerprint", "last_used")

    def __init__(self, client: MongoClient, digest: str) -> None:
        self.client = client
        self.fingerprint = digest
        self.last_used = time.monotonic()


class MongoPool:
    """One ``MongoClient`` per ``(user_id, connection_id)``."""

    def __init__(self) -> None:
        self._entries: dict[tuple[int, int], _Entry] = {}
        self._lock = threading.Lock()

    def client(self, user_id: int, conn_id: int) -> MongoClient:
        """
        The live client for a profile the caller owns, dialling if needed.

        The profile is re-read every time — one indexed SQLite lookup — so an
        edited profile takes effect on the next call instead of when the stale
        client happens to be evicted.
        """
        profile = fetch_connection(int(conn_id), int(user_id))
        if profile is None:
            raise ProfileNotFound("Connection not found")
        options = client_options(profile)
        digest = fingerprint(options)
        key = (int(user_id), int(conn_id))

        stale: list[MongoClient] = []
        with self._lock:
            stale.extend(self._evict_idle(keep=key))
            entry = self._entries.get(key)
            if entry is not None and entry.fingerprint != digest:
                stale.append(entry.client)
                entry = None
            if entry is None:
                entry = _Entry(MongoClient(**options), digest)
                self._entries[key] = entry
            entry.last_used = time.monotonic()
            client = entry.client
        for old in stale:
            _close_quietly(old)
        return client

    def close(self, user_id: int, conn_id: int) -> None:
        with self._lock:
            entry = self._entries.pop((int(user_id), int(conn_id)), None)
        if entry is not None:
            _close_quietly(entry.client)

    def close_user(self, user_id: int) -> None:
        with self._lock:
            keys = [key for key in self._entries if key[0] == int(user_id)]
            entries = [self._entries.pop(key) for key in keys]
        for entry in entries:
            _close_quietly(entry.client)

    def is_open(self, user_id: int, conn_id: int) -> bool:
        with self._lock:
            return (int(user_id), int(conn_id)) in self._entries

    def open_ids(self, user_id: int) -> list[int]:
        with self._lock:
            return [key[1] for key in self._entries if key[0] == int(user_id)]

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            _close_quietly(entry.client)

    def _evict_idle(self, keep: tuple[int, int]) -> list[MongoClient]:
        """Pop idle entries; the caller closes them outside the lock."""
        cutoff = time.monotonic() - _IDLE_TIMEOUT_SECONDS
        idle = [
            key for key, entry in self._entries.items()
            if key != keep and entry.last_used < cutoff
        ]
        return [self._entries.pop(key).client for key in idle]


def _close_quietly(client: MongoClient) -> None:
    try:
        client.close()
    except Exception:  # noqa: BLE001 - closing a dead client is not an error
        pass


pool = MongoPool()


def client_for(user_id: int, conn_id: int) -> MongoClient:
    return pool.client(user_id, conn_id)


def test_options(options: dict, timeout_ms: int = 5000) -> dict:
    """
    Dial a throwaway client and ping it. Never pooled: the editor's Test button
    runs this with values that may never be saved.
    """
    options = {**options, "serverSelectionTimeoutMS": timeout_ms}
    client: Optional[MongoClient] = None
    started = time.monotonic()
    try:
        client = MongoClient(**options)
        client.admin.command("ping")
        try:
            version = client.server_info().get("version", "")
        except Exception:  # noqa: BLE001 - some users may not run buildInfo
            version = ""
        elapsed = int((time.monotonic() - started) * 1000)
        return {"ok": True, "version": version, "ms": elapsed}
    except Exception as exc:  # noqa: BLE001 - reported to the person testing
        return {"ok": False, "error": error_text(exc)}
    finally:
        if client is not None:
            _close_quietly(client)


def error_text(exc: Exception) -> str:
    """
    pymongo's ServerSelectionTimeoutError runs to several hundred characters of
    topology description; the first clause is the useful part.
    """
    text = str(exc).strip() or exc.__class__.__name__
    text = text.split(", Timeout:")[0]
    return text.splitlines()[0][:400]
