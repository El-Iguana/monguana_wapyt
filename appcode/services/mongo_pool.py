"""
The client pool, and anything else that must outlive a single call.

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
import json
import threading
import time
from typing import Any

from services import backends
from services.db import fetch_connection

# Close a client that has gone unused for this long. It holds sockets on the
# server (and a monitor thread here) for every profile anyone ever opened.
_IDLE_TIMEOUT_SECONDS = 15 * 60


class ProfileNotFound(LookupError):
    pass


def fingerprint(backend_name: str, options: dict) -> str:
    """Changes whenever anything that affects the connection changes."""
    material = json.dumps([backend_name, options], sort_keys=True, default=str).encode()
    return hashlib.sha256(material).hexdigest()


class _Entry:
    __slots__ = ("client", "backend", "fingerprint", "last_used")

    def __init__(self, client: Any, backend: Any, digest: str) -> None:
        self.client = client
        self.backend = backend
        self.fingerprint = digest
        self.last_used = time.monotonic()


class MongoPool:
    """
    One client per ``(user_id, connection_id)``, built by the profile's
    backend (``services.backends``): a ``MongoClient`` for MongoDB.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[int, int], _Entry] = {}
        self._lock = threading.Lock()

    def client(self, user_id: int, conn_id: int) -> Any:
        return self.open(user_id, conn_id)[0]

    def open(self, user_id: int, conn_id: int) -> tuple[Any, Any]:
        """
        The live client for a profile the caller owns, dialling if needed,
        and the backend that built it (for its ``capabilities``).

        The profile is re-read every time — one indexed SQLite lookup — so an
        edited profile takes effect on the next call instead of when the stale
        client happens to be evicted.
        """
        profile = fetch_connection(int(conn_id), int(user_id))
        if profile is None:
            raise ProfileNotFound("Connection not found")
        backend = backends.for_profile(profile)
        options = backend.connect_options(profile)
        digest = fingerprint(backend.name, options)
        key = (int(user_id), int(conn_id))

        stale: list[Any] = []
        with self._lock:
            stale.extend(self._evict_idle(keep=key))
            entry = self._entries.get(key)
            if entry is not None and entry.fingerprint != digest:
                stale.append(entry.client)
                entry = None
            if entry is None:
                entry = _Entry(backend.open(options), backend, digest)
                self._entries[key] = entry
            entry.last_used = time.monotonic()
            client, built_by = entry.client, entry.backend
        for old in stale:
            close_quietly(old)
        return client, built_by

    def close(self, user_id: int, conn_id: int) -> None:
        with self._lock:
            entry = self._entries.pop((int(user_id), int(conn_id)), None)
        if entry is not None:
            close_quietly(entry.client)

    def close_user(self, user_id: int) -> None:
        with self._lock:
            keys = [key for key in self._entries if key[0] == int(user_id)]
            entries = [self._entries.pop(key) for key in keys]
        for entry in entries:
            close_quietly(entry.client)

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
            close_quietly(entry.client)

    def _evict_idle(self, keep: tuple[int, int]) -> list[Any]:
        """Pop idle entries; the caller closes them outside the lock."""
        cutoff = time.monotonic() - _IDLE_TIMEOUT_SECONDS
        idle = [
            key for key, entry in self._entries.items()
            if key != keep and entry.last_used < cutoff
        ]
        return [self._entries.pop(key).client for key in idle]


def close_quietly(client: Any) -> None:
    try:
        client.close()
    except Exception:  # noqa: BLE001 - closing a dead client is not an error
        pass


pool = MongoPool()


def client_for(user_id: int, conn_id: int) -> Any:
    return pool.client(user_id, conn_id)


def error_text(exc: Exception) -> str:
    """
    pymongo's ServerSelectionTimeoutError runs to several hundred characters of
    topology description; the first clause is the useful part.
    """
    text = str(exc).strip() or exc.__class__.__name__
    text = text.split(", Timeout:")[0]
    return text.splitlines()[0][:400]
