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
"""
from __future__ import annotations

import threading
from typing import Any

DEFAULT = "mongodb"


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
    from services.backends.mongodb import MongoBackend

    with _lock:
        _registry.setdefault(MongoBackend.name, MongoBackend())
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


def for_profile(profile: dict) -> Any:
    return get(profile.get("backend") or DEFAULT)
