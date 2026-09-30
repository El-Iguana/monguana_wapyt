"""
What a saved connection points at: a MongoDB server, or another backend.

A backend turns a saved profile into a PyMongo-shaped client. The pool
(``mongo_pool``) and the services never build clients themselves; they ask the
profile's backend. MongoDB is the built-in ``mongodb`` backend; ROADMAP phase
39 adds tinymongo next.

**Not a BFF module**, like ``mongo_pool``: the registry must be the same dict
on every call, and pytincture re-executes BFF modules per call.

A backend is any object with:

``name``, ``label``
    The stored identifier (``connections.backend``) and what people see.
``validate(profile) -> dict``
    Field name -> message, for the fields this backend owns. The profile's
    name is checked by the caller.
``connect_options(profile) -> dict``
    Everything that decides the connection, secrets included. The pool
    fingerprints it, so an edited profile gets a new client. JSON-able.
``open(options)``
    A PyMongo-shaped client for those options. Must not block on the network:
    the pool calls it under its lock.
``test(options) -> dict``
    Dial a throwaway client: ``{"ok": True, "version": str, "ms": int}`` or
    ``{"ok": False, "error": str}``. Never pooled.
``capabilities``
    The names from :data:`CAPABILITIES` this backend supports. MongoDB has
    them all. Without one, ``MongoService`` falls back to something simpler
    or refuses, and the UI hides what would be refused.
``fields() -> list | None``
    The connection editor's fields, as dicts (id, label, type, options,
    value, placeholder, help); ``None`` for MongoDB's own editor. Their values
    are saved as ``options``.
``public_options(options) -> dict``
    What of ``options`` the editor may show again. Never secrets.
``create_collection(database, name)`` (optional)
    How to make an empty collection where ``database.create_collection``
    does not exist.
"""
from __future__ import annotations

import threading
from typing import Any

DEFAULT = "mongodb"

# What a backend may lack, and what Monguana does without it.
CAPABILITIES = {
    "authorized_listing": "list_databases(authorizedDatabases=True); else a plain listing",
    "collection_types": "list_collections with types (views, timeseries); else names only",
    "create_collection": "explicit, empty collections and so new databases; else hidden",
    "capped": "capped collections; else hidden",
    "rename": "renaming a collection; else hidden",
    "stats": "$collStats sizes; else a document count only",
    "sample": "$sample; else the first documents",
    "explain": "query plans; else hidden",
    "time_limits": "maxTimeMS; else no server-side limit",
    "collmod": "in-place index changes and hidden indexes; else rebuilds, and no hiding",
    "ttl_indexes": "TTL indexes; else hidden",
    "dump_restore": "raw-BSON dump and restore; else hidden",
    "hello": "replica-set status; else none",
}


class BackendUnavailable(LookupError):
    """The profile names a backend that is not installed here."""


_registry: dict[str, Any] = {}
_lock = threading.Lock()
_builtins_loaded = False


def register(backend: Any) -> None:
    """Add a backend, replacing any registered under the same name."""
    with _lock:
        _registry[backend.name] = backend


def _load_builtins() -> None:
    global _builtins_loaded
    if _builtins_loaded:
        return
    from services.backends import tinymongo
    from services.backends.mongodb import MongoBackend

    with _lock:
        _registry.setdefault(MongoBackend.name, MongoBackend())
        # Only with MONGUANA_TINYMONGO_ROOT set (and tinymongo installed).
        if tinymongo.available():
            _registry.setdefault(tinymongo.TinyMongoBackend.name, tinymongo.TinyMongoBackend())
        _builtins_loaded = True


def get(name: str) -> Any:
    _load_builtins()
    key = (name or DEFAULT).strip()
    with _lock:
        backend = _registry.get(key)
    if backend is None:
        raise BackendUnavailable(f"The {key!r} backend is not installed")
    return backend


def names() -> list[str]:
    _load_builtins()
    with _lock:
        return sorted(_registry)


def listing() -> list[Any]:
    """Every backend, MongoDB first."""
    _load_builtins()
    with _lock:
        found = list(_registry.values())
    return sorted(found, key=lambda backend: (backend.name != DEFAULT, backend.label.lower()))


def for_profile(profile: dict) -> Any:
    return get(profile.get("backend") or DEFAULT)
