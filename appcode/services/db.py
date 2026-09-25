"""
SQLite storage and Fernet credential encryption. Server-side only.

Saved connection profiles hold MongoDB passwords and connection strings, so
both are encrypted at rest with a key that lives outside the database. Losing
``secret.key`` means losing every stored credential — back it up, or pin it
with ``MONGUANA_SECRET_KEY``.

The original Monguana kept connection passwords in plaintext and had a single
shared ``admin``/``admin`` login stored in an ``app_settings`` table. This is
multi-user: every profile belongs to one account, and there is no unscoped read
of the ``connections`` table anywhere.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import bcrypt
from cryptography.fernet import Fernet

DATA_DIR = Path(os.environ.get("MONGUANA_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
DB_PATH = DATA_DIR / "monguana.db"
KEY_PATH = DATA_DIR / "secret.key"
SESSION_KEY_PATH = DATA_DIR / "session.key"

_CRED_PREFIX = "fernet:"

# bcrypt cost. 12 is ~250ms per verify, which is the point; pytincture
# throttles login attempts on top of it.
_BCRYPT_ROUNDS = 12

_fernet: Optional[Fernet] = None
_fernet_lock = threading.Lock()


def _load_fernet() -> Fernet:
    """Resolve the encryption key once per process, behind a lock."""
    global _fernet
    if _fernet is not None:
        return _fernet
    with _fernet_lock:
        if _fernet is not None:
            return _fernet
        env_key = os.environ.get("MONGUANA_SECRET_KEY", "").strip()
        if env_key:
            _fernet = Fernet(env_key.encode())
            return _fernet
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if KEY_PATH.exists():
            _fernet = Fernet(KEY_PATH.read_bytes().strip())
            return _fernet
        key = Fernet.generate_key()
        # Created with the restrictive mode already in place: chmod after the
        # fact leaves a window where the key is world-readable.
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        _fernet = Fernet(key)
        return _fernet


def session_secret() -> str:
    """
    The cookie-signing secret for pytincture's session middleware.

    Persisted rather than generated per boot, or every restart would sign
    everybody out. ``MONGUANA_SESSION_SECRET`` overrides it.
    """
    from_env = os.environ.get("MONGUANA_SESSION_SECRET", "").strip()
    if from_env:
        return from_env
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SESSION_KEY_PATH.exists():
        existing = SESSION_KEY_PATH.read_text().strip()
        if existing:
            return existing
    secret = secrets.token_urlsafe(48)
    fd = os.open(SESSION_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.encode())
    finally:
        os.close(fd)
    return secret


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _CRED_PREFIX + _load_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_CRED_PREFIX):
        return value
    return _load_fernet().decrypt(value[len(_CRED_PREFIX):].encode()).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash    TEXT    NOT NULL,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- A connection profile. Either host/port/credentials, or a full connection
-- string in `uri` (Atlas, replica sets, anything mongodb+srv://), which then
-- wins. `uri` can embed a password, so it is encrypted like `password`.
CREATE TABLE IF NOT EXISTS connections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    folder       TEXT    NOT NULL DEFAULT '',
    host         TEXT    NOT NULL DEFAULT '',
    port         INTEGER NOT NULL DEFAULT 27017,
    username     TEXT    NOT NULL DEFAULT '',
    password     TEXT    NOT NULL DEFAULT '',
    auth_source  TEXT    NOT NULL DEFAULT 'admin',
    tls          INTEGER NOT NULL DEFAULT 0,
    direct       INTEGER NOT NULL DEFAULT 1,
    uri          TEXT    NOT NULL DEFAULT '',
    default_db   TEXT    NOT NULL DEFAULT '',
    notes        TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_connections_user ON connections(user_id);
"""


def init_db() -> None:
    """Create the schema and seed the first admin."""
    with get_db() as conn:
        conn.executescript(SCHEMA)

        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            admin_user = os.environ.get("MONGUANA_ADMIN_USER", "admin")
            admin_pass = os.environ.get("MONGUANA_ADMIN_PASS", "changeme")
            conn.execute(
                "INSERT INTO users (username, pw_hash, is_admin) VALUES (?, ?, 1)",
                (admin_user, hash_password(admin_pass)),
            )
            banner = "=" * 58
            # The password is deliberately not echoed into the log.
            print(
                f"\n{banner}\n"
                f"  Created the initial admin account: {admin_user!r}\n"
                f"  Using MONGUANA_ADMIN_PASS from the environment.\n"
                f"  Change it from the UI before exposing this service.\n"
                f"{banner}\n",
                flush=True,
            )


def fetch_connection(conn_id: int, user_id: int) -> Optional[dict]:
    """
    One profile with its secrets decrypted, or ``None``.

    Every caller scopes by ``user_id``: one user's saved servers are never
    reachable from another's session.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE id = ? AND user_id = ?",
            (int(conn_id), int(user_id)),
        ).fetchone()
    if not row:
        return None
    profile = dict(row)
    profile["password"] = decrypt(profile.get("password") or "")
    profile["uri"] = decrypt(profile.get("uri") or "")
    return profile
