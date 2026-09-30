"""
An example Monguana backend plugin: throwaway databases held in memory.

Each connection names a sandbox; connections naming the same one share it
until the Monguana server restarts, and then it is gone. Useful for trying
queries, and as a template: every method below is part of the backend
protocol (Monguana's ``services/backends/__init__.py``).

Install it next to Monguana and restart; ``python manage.py backends`` lists
it. See docs/BACKEND_PLUGINS.md.
"""
from __future__ import annotations

import re
import time
from typing import Any

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class SandboxBackend:
    name = "sandbox"
    label = "Sandbox (in memory)"

    # From Monguana's backends.CAPABILITIES; anything else is hidden or falls
    # back. tinymongo's memory engine lists databases and can make empty
    # collections (with the hook below), little more.
    capabilities = frozenset({"authorized_listing", "create_collection"})

    def fields(self) -> list:
        """The connection editor's fields. Their values arrive as ``options``."""
        return [
            {"id": "sandbox", "label": "Sandbox name", "value": "default",
             "help": "Connections with the same name share it, until the server restarts."},
        ]

    def public_options(self, options: dict) -> dict:
        """What the editor may show again. Nothing here is secret."""
        return {"sandbox": options.get("sandbox", "")}

    def validate(self, profile: dict) -> dict:
        """Field id -> message, for the fields above."""
        sandbox = (profile.get("options") or {}).get("sandbox", "")
        if not _NAME.match(sandbox or ""):
            return {"sandbox": "Letters, digits, - and _, at most 40"}
        return {}

    def connect_options(self, profile: dict) -> dict:
        """Everything that decides the connection; Monguana fingerprints it."""
        return {"sandbox": (profile.get("options") or {}).get("sandbox", "")}

    def open(self, options: dict) -> Any:
        """A PyMongo-shaped client. Must not block."""
        from tinymongo import MongoClient

        return MongoClient(tinymongo_folder=f"memory://monguana-sandbox-{options['sandbox']}",
                           backend="memory", tz_aware=True)

    def test(self, options: dict) -> dict:
        started = time.monotonic()
        try:
            client = self.open(options)
            count = len(client.list_database_names())
            client.close()
        except Exception as exc:  # noqa: BLE001 - shown to the person testing
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "version": f"sandbox {options['sandbox']} · {count} database(s)",
                "ms": int((time.monotonic() - started) * 1000)}

    @staticmethod
    def create_collection(database: Any, name: str) -> None:
        """Optional hook: tinymongo only creates a collection on its first insert."""
        collection = database[name]
        collection.delete_one({"_id": collection.insert_one({}).inserted_id})
