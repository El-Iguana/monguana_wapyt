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

**Other packages add backends** through the ``monguana.backends`` entry-point
group (phase 39, step 4): the entry point names a backend object, its class,
or a function returning one. It is loaded once, with the built-ins; a plugin
cannot replace ``mongodb`` or ``tinymongo``, and one that fails to load or
lacks part of the protocol is left out and reported in :data:`load_errors`
(``python manage.py backends``). Plugins run inside the server with its full
access, like any installed package. docs/BACKEND_PLUGINS.md has an example.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

DEFAULT = "mongodb"
ENTRY_POINT_GROUP = "monguana.backends"
BUILT_IN = ("mongodb", "tinymongo")

# What every backend must have. create_collection is optional.
REQUIRED = ("name", "label", "capabilities", "fields", "public_options",
            "validate", "connect_options", "open", "test")

log = logging.getLogger("monguana.backends")

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
_load_lock = threading.RLock()
_builtins_loaded = False
_loading = False
# Where each registered backend came from ("built-in" or "<dist> <version>").
sources: dict[str, str] = {}
# Plugins that were left out, and why.
load_errors: list[str] = []


def register(backend: Any) -> None:
    """Add a backend, replacing any registered under the same name."""
    with _lock:
        _registry[backend.name] = backend


def _load_builtins() -> None:
    """The built-ins, then the plugins, once. Callers wait until both are in."""
    global _builtins_loaded, _loading
    if _builtins_loaded:
        return
    with _load_lock:
        # _loading: a plugin that imports this registry while it is being
        # loaded (same thread, RLock) gets what is there so far.
        if _builtins_loaded or _loading:
            return
        _loading = True
        try:
            from services.backends import tinymongo
            from services.backends.mongodb import MongoBackend

            with _lock:
                _registry.setdefault(MongoBackend.name, MongoBackend())
                sources.setdefault(MongoBackend.name, "built-in")
                # Only with MONGUANA_TINYMONGO_ROOT set (and tinymongo installed).
                if tinymongo.available():
                    _registry.setdefault(tinymongo.TinyMongoBackend.name,
                                         tinymongo.TinyMongoBackend())
                    sources.setdefault(tinymongo.TinyMongoBackend.name, "built-in")
            _load_plugins()
            _builtins_loaded = True
        finally:
            _loading = False


def _entry_points() -> list:
    from importlib.metadata import entry_points

    return list(entry_points(group=ENTRY_POINT_GROUP))


def _load_plugins() -> None:
    """Every installed ``monguana.backends`` entry point that checks out."""
    try:
        found = _entry_points()
    except Exception as exc:  # noqa: BLE001 - broken metadata must not stop the app
        _reject("(entry points)", f"could not be listed: {exc}")
        return
    for entry in found:
        where = f"{entry.name} = {entry.value}"
        try:
            backend = _instantiate(entry.load())
        except Exception as exc:  # noqa: BLE001 - a broken plugin is reported, not fatal
            _reject(where, f"failed to load: {exc.__class__.__name__}: {exc}")
            continue
        problem = check(backend)
        if problem:
            _reject(where, problem)
            continue
        dist = getattr(entry, "dist", None)
        origin = f"{dist.name} {dist.version}" if dist is not None else entry.value
        with _lock:
            if backend.name in _registry:
                problem = f"the name {backend.name!r} is already taken ({sources.get(backend.name)})"
            else:
                _registry[backend.name] = backend
                sources[backend.name] = origin
        if problem:
            _reject(where, problem)
        else:
            log.info("backend %r loaded from %s", backend.name, origin)


def _instantiate(target: Any) -> Any:
    """A backend object from what an entry point names: the object, its class, or a factory."""
    if isinstance(target, type):
        return target()
    if all(hasattr(target, attr) for attr in REQUIRED):
        return target
    if callable(target):
        return target()
    return target


def check(backend: Any) -> str:
    """Why this object is not a usable backend, or ``""``."""
    missing = [attr for attr in REQUIRED if not hasattr(backend, attr)]
    if missing:
        return "missing " + ", ".join(missing)
    name = getattr(backend, "name")
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        return "name must be a non-empty string without surrounding spaces"
    if name in BUILT_IN:
        return f"{name!r} is a built-in backend and cannot be replaced"
    unknown = sorted(set(backend.capabilities) - set(CAPABILITIES))
    if unknown:
        return "unknown capabilities: " + ", ".join(unknown)
    return ""


def _reject(where: str, why: str) -> None:
    message = f"{where}: {why}"
    with _lock:
        load_errors.append(message)
    log.warning("backend plugin left out: %s", message)


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
