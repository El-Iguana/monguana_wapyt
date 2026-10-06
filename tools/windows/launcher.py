#!/usr/bin/env python3
"""
Monguana's launcher for a native (no Docker) install — ROADMAP phase 38.

The installer puts this next to ``service.py`` as ``monguana_launcher.py``
and points the Start-menu shortcut at it through ``pythonw.exe``. It:

* keeps one instance: a second launch just opens the browser on the first;
* picks port 8766, or the next free one up to 8799, on 127.0.0.2 only
  (``MONGUANA_HOST`` overrides it; see ``HOST`` below);
* on the first run, says how to sign in: ``admin`` / ``change_me``, which
  the app then asks you to change on every load until you do;
* starts ``service.py`` as a child process, logging to ``logs\\server.log``;
* opens ``http://127.0.0.2:<port>/monguana`` in the default browser —
  never ``localhost``, which pytincture answers with 400;
* sits in the tray (Open / Log folder / Quit) until Quit or the server dies.

Plain HTTP is fine because the address is a literal loopback IP: pytincture
allows an authenticated app over HTTP there and nowhere else.

    monguana_launcher.py                  start, or open the running instance
    monguana_launcher.py --no-browser     start without opening the browser
    monguana_launcher.py --no-tray        stay in the foreground (Ctrl+C stops)
    monguana_launcher.py --stop           stop the running instance
    monguana_launcher.py --status         exit 0 and print the URL if running
    monguana_launcher.py --check          start, verify the login page, stop
    monguana_launcher.py --reset-password reset an account's password

Data (SQLite, keys, logs) lives in ``%LOCALAPPDATA%\\Monguana`` on Windows,
``~/.local/share/monguana`` elsewhere, or ``MONGUANA_DATA_DIR``. It works the
same on Linux, which is how it is tested outside Windows CI.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
APPLICATION = "monguana"
FIRST_PORT, LAST_PORT = 8766, 8799
# Monguana's own loopback address. Browsers keep cookies per host, not per
# port; before pytincture 1.0.0rc13 every pytincture app named its session
# cookie the same, so next to IguanaXterm on 127.0.0.1 signing in to one signed
# you out of the other. service.py's cookie namespace now prevents that; the
# address stays for existing shortcuts. Windows and Linux answer on all of
# 127.0.0.0/8 with no setup.
HOST = os.environ.get("MONGUANA_HOST", "").strip() or "127.0.0.2"
START_TIMEOUT = 120  # seconds; the first start builds pytincture's browser assets
IS_WINDOWS = sys.platform == "win32"


def data_dir() -> Path:
    configured = os.environ.get("MONGUANA_DATA_DIR", "").strip()
    if configured:
        return Path(configured)
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Monguana"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "monguana"


DATA = data_dir()
STATE = DATA / "launcher.json"
LOGS = DATA / "logs"


# ── messages ────────────────────────────────────────────────────────────────

def message(text: str, title: str = "Monguana", error: bool = False) -> None:
    """A dialog on Windows (pythonw has no console), stdout elsewhere."""
    if IS_WINDOWS and not os.environ.get("MONGUANA_LAUNCHER_QUIET"):
        import ctypes

        # MB_OK | MB_SETFOREGROUND | MB_TOPMOST, plus the icon. A message box
        # also copies its text with Ctrl+C.
        flags = 0x0 | 0x10000 | 0x40000 | (0x10 if error else 0x40)
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
        return
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(text, file=stream, flush=True)


# ── the running instance ────────────────────────────────────────────────────

def url(port: int) -> str:
    return f"http://{HOST}:{port}/{APPLICATION}"


def healthy(port: int, timeout: float = 2.0) -> bool:
    request = urllib.request.Request(
        f"http://{HOST}:{port}/healthz", headers={"Host": f"{HOST}:{port}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def read_state() -> dict | None:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(state, dict) and isinstance(state.get("port"), int) and healthy(state["port"]):
        return state
    return None


def write_state(port: int, server_pid: int) -> None:
    STATE.write_text(
        json.dumps({"port": port, "server_pid": server_pid, "launcher_pid": os.getpid()}),
        encoding="utf-8",
    )


def clear_state(server_pid: int) -> None:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if state.get("server_pid") == server_pid:
            STATE.unlink()
    except (OSError, ValueError):
        pass


def free_port() -> int | None:
    for port in range(FIRST_PORT, LAST_PORT + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, port))
            except OSError:
                continue
            return port
    return None


def stop_pid(pid: int) -> None:
    """Stop the server; its launcher notices and leaves the tray by itself."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


# ── starting the server ─────────────────────────────────────────────────────

def server_python() -> str:
    """python.exe, not pythonw.exe: its output has to reach the log file."""
    exe = Path(sys.executable)
    if IS_WINDOWS and exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(exe)


def start_server(port: int) -> subprocess.Popen:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / "server.log"
    if log.exists():
        try:
            log.replace(LOGS / "server.previous.log")
        except OSError:
            pass
    env = dict(os.environ)
    # tinymongo stores (ROADMAP phase 39): on unless the person chose a root.
    tinymongo_root = os.environ.get("MONGUANA_TINYMONGO_ROOT", "").strip() or str(DATA / "tinymongo")
    Path(tinymongo_root).mkdir(parents=True, exist_ok=True)
    env.update({
        "MONGUANA_DATA_DIR": str(DATA),
        "MONGUANA_TINYMONGO_ROOT": tinymongo_root,
        "MONGUANA_HOST": HOST,
        "MONGUANA_BIND": HOST,
        "PORT": str(port),
        "MONGUANA_CANONICAL_ORIGIN": f"http://{HOST}:{port}",
        "MONGUANA_ALLOWED_HOSTS": HOST,
        "PYTHONUNBUFFERED": "1",
        # UTF-8 for open() and the log, whatever the Windows code page is.
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    # The first account gets the default password (change_me), not one
    # left in this machine's environment by something else.
    env.pop("MONGUANA_ADMIN_USER", None)
    env.pop("MONGUANA_ADMIN_PASS", None)
    flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    with open(log, "ab") as out:
        return subprocess.Popen(
            [server_python(), str(HERE / "service.py")],
            cwd=str(HERE), env=env, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=flags,
        )


def wait_until_up(server: subprocess.Popen, port: int) -> bool:
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if server.poll() is not None:
            return False
        if healthy(port, timeout=1.0):
            return True
        time.sleep(0.3)
    return False


def stop_server(server: subprocess.Popen) -> None:
    if server.poll() is not None:
        return
    if IS_WINDOWS:
        stop_pid(server.pid)
    else:
        server.terminate()
    try:
        server.wait(timeout=15)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def launch() -> tuple[subprocess.Popen, int] | None:
    """Start the server and wait for it; ``None`` after telling the user why not."""
    DATA.mkdir(parents=True, exist_ok=True)
    port = free_port()
    if port is None:
        message(f"No free port between {FIRST_PORT} and {LAST_PORT} on {HOST}.", error=True)
        return None
    first_run = not (DATA / "monguana.db").exists()
    server = start_server(port)
    if not wait_until_up(server, port):
        stop_server(server)
        message(
            "Monguana did not start. The server log says why:\n\n"
            f"{LOGS / 'server.log'}", error=True,
        )
        return None
    write_state(port, server.pid)
    if first_run:
        message(
            "Monguana is ready. Sign in with:\n\n"
            "Username:  admin\nPassword:  change_me\n\n"
            "Monguana will ask you to choose your own password each time it "
            "loads until you do. Forgot it later? Use \"Reset Monguana "
            "password\" in the Start menu.",
            title="Monguana — first run",
        )
    return server, port


# ── the tray ────────────────────────────────────────────────────────────────

def open_folder(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: S606 - a folder, opened in Explorer
    else:
        webbrowser.open(path.as_uri())


def run_tray(server: subprocess.Popen, port: int) -> None:
    """Tray icon until Quit, or until the server exits on its own."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        run_foreground(server)
        return

    def on_open(_icon=None, _item=None) -> None:
        webbrowser.open(url(port))

    def on_logs(_icon=None, _item=None) -> None:
        open_folder(LOGS)

    def on_quit(icon, _item=None) -> None:
        stop_server(server)
        icon.stop()

    image = Image.open(HERE / "appcode" / "static" / "el_iguana_avatar.webp")
    icon = pystray.Icon(
        "monguana", image, f"Monguana — {url(port)}",
        menu=pystray.Menu(
            pystray.MenuItem("Open Monguana", on_open, default=True),
            pystray.MenuItem("Show log folder", on_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Monguana", on_quit),
        ),
    )

    def watch() -> None:
        server.wait()
        icon.stop()

    threading.Thread(target=watch, daemon=True).start()
    icon.run()


def run_foreground(server: subprocess.Popen) -> None:
    try:
        server.wait()
    except KeyboardInterrupt:
        stop_server(server)


# ── commands ────────────────────────────────────────────────────────────────

def cmd_start(args) -> int:
    running = read_state()
    if running:
        if not args.no_browser:
            webbrowser.open(url(running["port"]))
        return 0
    started = launch()
    if started is None:
        return 1
    server, port = started
    try:
        if not args.no_browser:
            webbrowser.open(url(port))
        if args.no_tray:
            print(f"Monguana is running on {url(port)}", flush=True)
            run_foreground(server)
        else:
            run_tray(server, port)
    finally:
        stop_server(server)
        clear_state(server.pid)
    return 0


def cmd_stop(_args) -> int:
    running = read_state()
    if running:
        stop_pid(running["server_pid"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and healthy(running["port"], timeout=0.5):
            time.sleep(0.3)
    try:
        STATE.unlink()
    except OSError:
        pass
    return 0


def cmd_status(_args) -> int:
    running = read_state()
    if running:
        print(url(running["port"]))
        return 0
    print("Monguana is not running.")
    return 1


def cmd_check(_args) -> int:
    """For CI and support: start, fetch the login page, stop. Never prompts."""
    os.environ["MONGUANA_LAUNCHER_QUIET"] = "1"
    if read_state():
        print("Monguana is already running; --check needs it stopped.", file=sys.stderr)
        return 1
    started = launch()
    if started is None:
        return 1
    server, port = started
    try:
        request = urllib.request.Request(url(port), headers={"Host": f"{HOST}:{port}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read().decode("utf-8", "replace")
        # The login page, with Monguana's "Username" rewrite applied.
        ok = response.status == 200 and "Username" in page and "password" in page.lower()
        print(f"{url(port)} -> {response.status}, login page {'found' if ok else 'MISSING'}")
        return 0 if ok else 1
    except (OSError, urllib.error.URLError) as exc:
        print(f"{url(port)} -> {exc}", file=sys.stderr)
        return 1
    finally:
        stop_server(server)
        clear_state(server.pid)


def cmd_reset_password(_args) -> int:
    """Console only: the Start-menu shortcut runs this through python.exe."""
    os.environ["MONGUANA_DATA_DIR"] = str(DATA)
    sys.path.insert(0, str(HERE))
    import manage

    username = input("Account to reset [admin]: ").strip() or "admin"
    code = manage.main(["reset-password", username])
    if IS_WINDOWS:
        input("\nPress Enter to close this window.")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="monguana_launcher", description="Start Monguana.")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--no-tray", action="store_true", help="stay in the foreground")
    action = parser.add_mutually_exclusive_group()
    for flag in ("stop", "status", "check", "reset-password"):
        action.add_argument(f"--{flag}", action="store_true")
    args = parser.parse_args(argv)
    if args.stop:
        return cmd_stop(args)
    if args.status:
        return cmd_status(args)
    if args.check:
        return cmd_check(args)
    if args.reset_password:
        return cmd_reset_password(args)
    return cmd_start(args)


if __name__ == "__main__":
    sys.exit(main())
