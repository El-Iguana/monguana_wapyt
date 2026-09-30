"""
Backends from other packages, through the ``monguana.backends`` entry point
(phase 39, step 4).

The example plugin in ``examples/monguana-sandbox-backend`` is "installed" the
way pip lays it out: a ``.dist-info`` with its entry points, generated from
the example's own pyproject.toml, on ``sys.path``. So the real
``importlib.metadata`` discovery runs, and the documented example is the one
that works.
"""
from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("tinymongo")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "monguana-sandbox-backend"


@pytest.fixture()
def registry():
    """A registry that loads again (built-ins and plugins), restored afterwards."""
    from services import backends

    with backends._lock:
        saved = (dict(backends._registry), dict(backends.sources),
                 list(backends.load_errors), backends._builtins_loaded)
        backends._registry.clear()
        backends.sources.clear()
        backends.load_errors.clear()
        backends._builtins_loaded = False
    yield backends
    with backends._lock:
        backends._registry.clear()
        backends._registry.update(saved[0])
        backends.sources.clear()
        backends.sources.update(saved[1])
        backends.load_errors[:] = saved[2]
        backends._builtins_loaded = saved[3]


def _install(site: Path, dist: str, version: str, entry_points: dict, modules: dict) -> None:
    """Lay out an installed distribution: modules plus a .dist-info."""
    for name, source in modules.items():
        (site / f"{name}.py").write_text(source, encoding="utf-8")
    info = site / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n", encoding="utf-8")
    lines = ["[monguana.backends]", *(f"{key} = {value}" for key, value in entry_points.items())]
    (info / "entry_points.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def site(tmp_path, monkeypatch):
    folder = tmp_path / "site-packages"
    folder.mkdir()
    monkeypatch.syspath_prepend(str(folder))
    yield folder
    importlib.invalidate_caches()
    for name in [name for name in sys.modules if name.startswith(("monguana_sandbox", "plug_"))]:
        del sys.modules[name]


@pytest.fixture()
def sandbox_installed(site):
    project = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    module = "monguana_sandbox_backend"
    _install(site, project["name"], project["version"],
             project["entry-points"]["monguana.backends"],
             {module: (EXAMPLE / f"{module}.py").read_text(encoding="utf-8")})
    importlib.invalidate_caches()


def test_the_example_plugin_is_discovered(registry, sandbox_installed):
    assert "sandbox" in registry.names()
    assert registry.sources["sandbox"] == "monguana-sandbox-backend 0.1.0"
    assert registry.load_errors == []
    assert [row.name for row in registry.listing()][0] == "mongodb"


def test_the_example_plugin_works_through_the_services(registry, sandbox_installed, tmp_path):
    from services import db

    saved = (db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet)
    db.DATA_DIR, db.DB_PATH = tmp_path / "data", tmp_path / "data" / "monguana.db"
    db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = tmp_path / "k", tmp_path / "s", None
    try:
        db.init_db()
        from services.connection_service import ConnectionService
        from services.mongo_pool import pool
        from services.mongo_service import MongoService

        user = {"user_id": 1}
        conns, mongo = ConnectionService(user), MongoService(user)
        assert conns.save(name="x", backend="sandbox", options={"sandbox": "no spaces"})[
            "errors"] == {"sandbox": "Letters, digits, - and _, at most 40"}
        saved_conn = conns.save(name="play", backend="sandbox", options={"sandbox": "t1"})
        conn = saved_conn["id"]
        assert conns.test(conn_id=conn, backend="sandbox")["version"].startswith("sandbox t1")
        assert any(row["name"] == "sandbox" for row in conns.backends())

        assert mongo.create_database(conn, "shop", "orders")["ok"]
        assert mongo.insert(conn, "shop", "orders", "[{n: 1}, {n: 2}]")["count"] == 2
        found = mongo.find(conn, "shop", "orders", "{n: {$gt: 1}}")
        assert [row["doc"]["n"] for row in found["docs"]] == [2]
        assert mongo.explain(conn, "shop", "orders")["error"] == \
            "Sandbox (in memory) connections cannot explain queries"
        row = next(row for row in conns.list() if row["id"] == conn)
        assert row["capabilities"] == ["authorized_listing", "create_collection"]
        pool.close_all()
    finally:
        db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = saved


BROKEN = {
    "plug_raises": ("boom = plug_raises:Backend", "class Backend:\n    def __init__(self):\n"
                    "        raise RuntimeError('no config')\n", "failed to load: RuntimeError"),
    "plug_partial": ("partial = plug_partial:Backend", "class Backend:\n    name = 'partial'\n",
                     "missing label"),
    "plug_builtin": ("builtin = plug_builtin:make", None, "built-in backend and cannot be replaced"),
    "plug_caps": ("caps = plug_caps:make", None, "unknown capabilities: teleport"),
}


def _full_backend_source(name: str, caps: str) -> str:
    source = (EXAMPLE / "monguana_sandbox_backend.py").read_text(encoding="utf-8")
    return (source.replace('name = "sandbox"', f'name = "{name}"')
            .replace('frozenset({"authorized_listing", "create_collection"})', caps)
            + "\n\ndef make():\n    return SandboxBackend()\n")


def test_broken_plugins_are_left_out_and_reported(registry, site):
    modules = {}
    points = {}
    for module, (point, source, _) in BROKEN.items():
        key, value = point.split(" = ")
        points[key] = value
        if source is None:
            source = _full_backend_source(
                "mongodb" if module == "plug_builtin" else "caps",
                'frozenset({"teleport"})' if module == "plug_caps" else "frozenset()")
        modules[module] = source
    # Two plugins claiming one name: the second is refused.
    modules["plug_twin_a"] = _full_backend_source("twin", "frozenset()")
    modules["plug_twin_b"] = _full_backend_source("twin", "frozenset()")
    points["twin_a"] = "plug_twin_a:make"
    points["twin_b"] = "plug_twin_b:SandboxBackend"
    _install(site, "broken-plugins", "1.0", points, modules)
    importlib.invalidate_caches()

    names = registry.names()
    assert "twin" in names and "partial" not in names and "caps" not in names
    assert registry.get("mongodb").__class__.__name__ == "MongoBackend"
    errors = "\n".join(registry.load_errors)
    for _, _, expected in BROKEN.values():
        assert expected in errors, errors
    assert "'twin' is already taken (broken-plugins 1.0)" in errors


def test_manage_lists_plugins_and_fails_on_load_errors(registry, site, capsys):
    import manage

    _install(site, "half", "1.0", {"half": "plug_half:Backend"},
             {"plug_half": "class Backend:\n    name = 'half'\n"})
    importlib.invalidate_caches()
    assert manage.backends_cmd() == 1
    out = capsys.readouterr().out
    assert out.startswith("mongodb ")
    assert "left out: half = plug_half:Backend: missing label" in out
