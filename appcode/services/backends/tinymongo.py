"""
tinymongo: MongoDB-shaped stores in local files (phase 39, step 3).

tinymongo has no wire protocol, so the store is opened **in-process**, on the
server Monguana runs on. Its ``MongoClient`` is PyMongo-shaped; what it lacks
is declared in :attr:`TinyMongoBackend.capabilities` and handled by
``MongoService`` (checked against tinymongo 1.3.1 and master, 2026-09-30).

**The folder is a path on the server**, so it is confined to one directory the
administrator chose, ``MONGUANA_TINYMONGO_ROOT``. Without it the backend is not
registered at all. Profiles store the folder *relative* to that root, and every
use re-resolves it (symlinks included) and refuses anything outside.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

ROOT_ENV = "MONGUANA_TINYMONGO_ROOT"

# tinymongo's storage engines offered here, as (tinymongo name, label). SQLite
# and JSON need no extras; duckdb/parquet come later (phase 39, step 4).
ENGINES = (("sqlite", "SQLite"), ("json", "JSON"))


def configured_root() -> Optional[Path]:
    """The root folder, or None when the backend is switched off."""
    value = os.environ.get(ROOT_ENV, "").strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


def available() -> bool:
    if configured_root() is None:
        return False
    try:
        import tinymongo  # noqa: F401
    except ImportError:
        return False
    return True


class OutsideRoot(ValueError):
    pass


def resolve_folder(folder: str, root: Optional[Path] = None) -> Path:
    """
    The absolute folder for a profile's relative one, which must exist and be
    inside the root once symlinks are resolved.
    """
    root = root or configured_root()
    if root is None:
        raise OutsideRoot(f"tinymongo is switched off ({ROOT_ENV} is not set)")
    text = (folder or "").strip().replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise OutsideRoot("Give the folder relative to the tinymongo root")
    candidate = (root / text).resolve() if text not in ("", ".") else root
    if candidate != root and root not in candidate.parents:
        raise OutsideRoot("That folder is outside the tinymongo root")
    if not candidate.is_dir():
        raise OutsideRoot("That folder does not exist under the tinymongo root")
    return candidate


class TinyMongoBackend:
    name = "tinymongo"
    label = "tinymongo"

    # Everything else is missing: no list_collections, rename, $sample,
    # $collStats, explain, collMod or raw BSON, and aggregate() refuses
    # maxTimeMS. TTL indexes are accepted but never expire, so they are not
    # offered. Collections exist from their first insert; the hook below makes
    # "Create collection" work anyway.
    capabilities = frozenset({"authorized_listing", "create_collection"})

    def fields(self) -> list:
        """The connection editor's fields for this backend (see ConnectionService.backends)."""
        return [
            {"id": "engine", "label": "Storage engine", "type": "select",
             "options": [{"value": value, "label": label} for value, label in ENGINES],
             "value": ENGINES[0][0]},
            {"id": "folder", "label": "Store folder", "value": "",
             "placeholder": "(the root itself)",
             # Not the root's absolute path: every signed-in user sees this.
             "help": "Relative to the tinymongo root your administrator set; "
                     "it must exist. Each database is one file in it."},
        ]

    def public_options(self, options: dict) -> dict:
        """What the editor may show again: nothing here is secret."""
        return {"engine": options.get("engine", ""), "folder": options.get("folder", "")}

    def validate(self, profile: dict) -> dict:
        options = profile.get("options") or {}
        errors: dict = {}
        if options.get("engine") not in dict(ENGINES):
            errors["engine"] = "Choose " + " or ".join(label for _, label in ENGINES)
        try:
            resolve_folder(options.get("folder", ""))
        except OutsideRoot as exc:
            errors["folder"] = str(exc)
        return errors

    def connect_options(self, profile: dict) -> dict:
        options = profile.get("options") or {}
        return {
            "engine": options.get("engine", ""),
            "path": str(resolve_folder(options.get("folder", ""))),
        }

    def open(self, options: dict) -> Any:
        from tinymongo import MongoClient

        # Checked again at open: the profile was validated when saved, but the
        # folder may have been replaced by a symlink since.
        root = configured_root()
        path = Path(options["path"])
        if root is None or (path.resolve() != root and root not in path.resolve().parents):
            raise OutsideRoot("That folder is outside the tinymongo root")
        return MongoClient(tinymongo_folder=str(path), backend=options["engine"], tz_aware=True)

    def test(self, options: dict) -> dict:
        """
        Open the store and list its databases. Not ``admin.command("ping")``:
        on the JSON engine, touching a database creates its file.
        """
        from services.mongo_pool import close_quietly, error_text

        client = None
        started = time.monotonic()
        try:
            client = self.open(options)
            count = len(client.list_database_names())
            elapsed = int((time.monotonic() - started) * 1000)
            return {"ok": True, "version": f"{_version()} · {options['engine']} · "
                    f"{count} database(s)", "ms": elapsed}
        except Exception as exc:  # noqa: BLE001 - reported to the person testing
            return {"ok": False, "error": error_text(exc)}
        finally:
            if client is not None:
                close_quietly(client)

    @staticmethod
    def create_collection(database: Any, name: str) -> None:
        """
        tinymongo creates a collection on its first insert. One placeholder,
        deleted at once, leaves it existing and empty (both engines keep it).
        """
        collection = database[name]
        inserted = collection.insert_one({"_monguana_create": True}).inserted_id
        collection.delete_one({"_id": inserted})


def _version() -> str:
    try:
        from importlib.metadata import version

        return f"tinymongo {version('tinymongo')}"
    except Exception:  # noqa: BLE001 - a source checkout has no metadata
        return "tinymongo"
