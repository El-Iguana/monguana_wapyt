"""tools/windows/launcher.py — the native install's launcher (phase 38)."""
from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools" / "windows" / "launcher.py"


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGUANA_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("monguana_launcher_under_test", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_data_dir_follows_the_environment(launcher, tmp_path):
    assert launcher.DATA == tmp_path
    assert launcher.STATE == tmp_path / "launcher.json"


def test_default_data_dir_is_per_user(launcher, monkeypatch, tmp_path):
    monkeypatch.delenv("MONGUANA_DATA_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    expected = tmp_path / "local" / "Monguana" if sys.platform == "win32" else tmp_path / "xdg" / "monguana"
    assert launcher.data_dir() == expected


def test_the_url_is_the_loopback_ip_never_localhost(launcher):
    assert launcher.url(8766) == "http://127.0.0.1:8766/monguana"


def test_free_port_skips_a_taken_one(launcher, monkeypatch):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
        monkeypatch.setattr(launcher, "FIRST_PORT", port)
        monkeypatch.setattr(launcher, "LAST_PORT", port + 20)
        chosen = launcher.free_port()
    assert chosen is not None and port < chosen <= port + 20


def test_a_stale_state_file_is_not_a_running_instance(launcher):
    launcher.STATE.write_text('{"port": 1, "server_pid": 1, "launcher_pid": 1}', encoding="utf-8")
    assert launcher.read_state() is None
    assert launcher.main(["--status"]) == 1
