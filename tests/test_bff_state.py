"""
State must survive pytincture re-executing a BFF module on every call.

pytincture loads a BFF module's source afresh for each call, so a pool or
registry defined in such a module is a new, empty one per request —
IguanaXterm's SFTP pool dialled on every click because of it. These load the
modules the way pytincture does, twice.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APPCODE = Path(__file__).resolve().parents[1] / "appcode"

loading = pytest.importorskip("pytincture.backend.source_loading")


def _load_like_pytincture(relative: str, hint: str):
    return loading.load_source_module(str(APPCODE / relative), hint, str(APPCODE))


def test_the_client_pool_is_the_same_pool_on_every_call():
    from services.mongo_pool import pool

    for relative, hint in (
        ("services/mongo_service.py", "MongoService"),
        ("services/connection_service.py", "ConnectionService"),
    ):
        first = _load_like_pytincture(relative, hint)
        second = _load_like_pytincture(relative, hint)
        assert first is not second, "pytincture really does re-execute the module"
        # The modules import the pool inside functions, so check what they get.
        import services.mongo_pool as via_import

        assert via_import.pool is pool


def test_no_bff_module_keeps_mutable_state_at_module_level():
    suspicious = []
    for path in (APPCODE / "services").glob("*.py"):
        tree = ast.parse(path.read_text())
        exports_bff = any(
            isinstance(node, ast.ClassDef)
            and any(getattr(d, "id", None) == "backend_for_frontend" for d in node.decorator_list)
            for node in tree.body
        )
        if not exports_bff:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if isinstance(value, (ast.Dict, ast.List, ast.Set)) or isinstance(value, ast.Call):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                suspicious.append(f"{path.name}: {', '.join(getattr(t, 'id', '?') for t in targets)}")
    assert suspicious == [], "state in a BFF module is rebuilt on every call -- move it to mongo_pool"


def test_bff_modules_use_absolute_imports():
    """pytincture imports BFF modules by path: `from .x import` fails at call time."""
    for path in (APPCODE / "services").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path.name}: relative import"
