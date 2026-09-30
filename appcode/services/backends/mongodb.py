"""
The built-in backend: a MongoDB server, through PyMongo.

Moved here from ``mongo_pool`` and ``ConnectionService`` (phase 39, step 1)
without changing what it does.
"""
from __future__ import annotations

import time
from typing import Any

APP_NAME = "Monguana"

_URI_SCHEMES = ("mongodb://", "mongodb+srv://")


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


class MongoBackend:
    name = "mongodb"
    label = "MongoDB"

    def validate(self, profile: dict) -> dict:
        """The Form checks the same things in the browser; this is the copy that counts."""
        errors: dict = {}
        uri = (profile.get("uri") or "").strip()
        if uri:
            if not uri.startswith(_URI_SCHEMES):
                errors["uri"] = "A connection string starts with mongodb:// or mongodb+srv://"
            return errors
        if not (profile.get("host") or "").strip():
            errors["host"] = "Host is required (or give a connection string)"
        try:
            port_value = int(profile.get("port"))
            if not 1 <= port_value <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            errors["port"] = "Port must be between 1 and 65535"
        return errors

    def connect_options(self, profile: dict) -> dict:
        return client_options(profile)

    def open(self, options: dict) -> Any:
        from pymongo import MongoClient

        # MongoClient connects in the background; constructing it does not block.
        return MongoClient(**options)

    def test(self, options: dict, timeout_ms: int = 5000) -> dict:
        """
        Dial a throwaway client and ping it. Never pooled: the editor's Test
        button runs this with values that may never be saved.
        """
        from services.mongo_pool import close_quietly, error_text

        options = {**options, "serverSelectionTimeoutMS": timeout_ms}
        client = None
        started = time.monotonic()
        try:
            client = self.open(options)
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
                close_quietly(client)
