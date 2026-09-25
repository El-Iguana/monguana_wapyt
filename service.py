#!/usr/bin/env python3
"""
ASGI entrypoint for Monguana.

    python service.py
    # or: uvicorn service:app --host 0.0.0.0 --port 8766

Everything server-side is wired here rather than in the application module, so
``appcode/monguana.py`` stays pure browser code: pytincture serves that
module's source to Pyodide, and a server-only import in it is still visible to
the AST pass that resolves the widgetset and the entrypoint.

Most of this is IguanaXterm's ``service.py`` with the names changed; the
reasons behind each piece are recorded there and in CLAUDE.md.
"""
from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
APPCODE = HERE / "appcode"

# The BFF modules import each other as `services.<name>`, and pytincture loads
# them by path from the modules folder, so that folder must be importable here.
sys.path.insert(0, str(APPCODE))


def load_dotenv_file() -> None:
    """Load ``.env`` from the project root. Real environment variables win."""
    env_path = HERE / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv_file()

from pytincture import PytinctureConfig, create_app  # noqa: E402
from pytincture.backend.middleware import RequestBodyLimitMiddleware  # noqa: E402

from services.db import init_db, session_secret  # noqa: E402
from services.login_page import LoginPageMiddleware, check_at_startup  # noqa: E402
from services.mongo_pool import pool  # noqa: E402
from services.transfer import max_restore_bytes, router as transfer_router  # noqa: E402

APPLICATION = "monguana"
PORT = int(os.getenv("PORT", "8766"))
# The address uvicorn listens on. The container runs with host networking (a
# database GUI has to reach servers on the host's loopback and LAN), and there
# 0.0.0.0 would publish plain HTTP to the whole network: podman-run.sh sets
# 127.0.0.1.
BIND = os.getenv("MONGUANA_BIND", "0.0.0.0")


def canonical_origin() -> str:
    """
    The single origin this service is reached on. The default is the literal
    IP, not "localhost": pytincture's loopback check parses it as an address.
    """
    return os.getenv("MONGUANA_CANONICAL_ORIGIN", f"http://127.0.0.1:{PORT}").rstrip("/")


def _strip_port(value: str) -> str:
    head, _, tail = value.rpartition(":")
    return head if head and tail.isdigit() else value


def allowed_hosts() -> tuple[str, ...]:
    """
    Exact Host values this service answers to, **hostnames only** (Starlette
    compares with the port stripped). The canonical origin's host is always
    included, because pytincture refuses a configuration without it.
    """
    configured = os.getenv("MONGUANA_ALLOWED_HOSTS", "").strip()
    if configured:
        hosts = [_strip_port(part.strip()) for part in configured.split(",") if part.strip()]
    else:
        hosts = ["127.0.0.1"]
    canonical = urlsplit(canonical_origin()).hostname
    if canonical and canonical not in hosts:
        hosts.append(canonical)
    return tuple(dict.fromkeys(hosts))


def is_loopback_deployment(origin: str) -> bool:
    parsed = urlsplit(origin)
    if parsed.scheme == "https":
        return False
    try:
        return ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        return False


# pytincture's own cap on every request body. Kept for everything but restore.
_DEFAULT_BODY_LIMIT = 2 * 1024 * 1024

_RESTORE_PATH = re.compile(r"/mg/restore/\d+")


class BodyLimitExceptRestore:
    """
    pytincture's 2 MiB body limit on every path except the restore upload.

    pytincture applies one limit to the whole app, so a dump over 2 MiB would
    be refused with 413 before the restore route ever ran. The app-wide limit
    is raised to the restore cap and this puts the original back, outermost,
    on everything else. The restore route authenticates and checks CSRF before
    reading a byte, and enforces its own cap while streaming.
    """

    def __init__(self, app, max_bytes: int = _DEFAULT_BODY_LIMIT) -> None:
        self.app = app
        self.limited = RequestBodyLimitMiddleware(app, max_bytes)

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and _RESTORE_PATH.fullmatch(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
        else:
            await self.limited(scope, receive, send)


def build_app():
    init_db()
    # Job files (dumps, uploaded restores) belong to jobs that died with the
    # last process.
    from services.jobs import clear_leftovers

    clear_leftovers()

    # Registered by dotted path, not by setter: create_app() loads its own
    # isolated backend module, which a setter called against the shared one
    # never reaches. AUTH_SESSION_CLAIM_KEYS is what keeps user_id in the
    # session; without it every BFF call is a silent 403.
    hooks = {
        "AUTH_USER_AUTHENTICATOR": "services.auth.authenticate",
        "BFF_POLICY_HOOK_PATH": "services.auth.policy_hook",
        "AUTH_SESSION_CLAIM_KEYS": "user_id,is_admin,username",
    }

    origin = canonical_origin()
    loopback = is_loopback_deployment(origin)
    if loopback:
        print(
            "  Loopback development mode: plain HTTP on localhost.\n"
            "  Set MONGUANA_CANONICAL_ORIGIN to an https:// URL (and put TLS in\n"
            "  front) before exposing this to anything but your own machine.",
            flush=True,
        )

    application = create_app(
        PytinctureConfig(
            modules_path=str(APPCODE),
            default_application=APPLICATION,
            enable_user_login=True,
            # Only unlocks loopback plain HTTP. pytincture's password-less
            # branch is unreachable once AUTH_USER_AUTHENTICATOR is set.
            enable_dev_email_login=loopback,
            session_secret=session_secret(),
            allowed_hosts=allowed_hosts(),
            canonical_origin=origin,
            trusted_proxy_headers=not loopback,
            max_request_body_bytes=max(max_restore_bytes(), _DEFAULT_BODY_LIMIT),
            environment=hooks,
        )
    )

    check_at_startup()
    application.add_middleware(LoginPageMiddleware)
    application.include_router(transfer_router)

    # Same-origin artwork: pytincture's CSP would allow https: images, but
    # local keeps the branding working on an air-gapped deployment.
    from fastapi.staticfiles import StaticFiles

    application.mount(
        "/static", StaticFiles(directory=str(APPCODE / "static")), name="static"
    )

    # Third-party browser code, vendored because the CSP allows scripts from
    # 'self' only. The editor bundle is loaded on demand; see ROADMAP phase 31
    # and tools/codemirror/.
    application.mount(
        "/vendor", StaticFiles(directory=str(APPCODE / "vendor")), name="vendor"
    )

    @application.on_event("shutdown")
    async def _close_pool() -> None:
        pool.close_all()

    return BodyLimitExceptRestore(application)


app = build_app()


if __name__ == "__main__":
    import uvicorn

    print(
        f"""
  __  __
 |  \\/  | ___  _ __   __ _ _   _  __ _ _ __   __ _
 | |\\/| |/ _ \\| '_ \\ / _` | | | |/ _` | '_ \\ / _` |
 | |  | | (_) | | | | (_| | |_| | (_| | | | | (_| |
 |_|  |_|\\___/|_| |_|\\__, |\\__,_|\\__,_|_| |_|\\__,_|
                     |___/
  Browser-based MongoDB manager — pytincture/wapyt
  Developer : OldManGan <eliguana@protonmail.com>
  URL       : {canonical_origin()}/{APPLICATION}
""",
        flush=True,
    )
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
