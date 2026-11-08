"""Passwort (scrypt), serverseitige Sessions und Bremse gegen Passwort-Raten."""
import base64
import hashlib
import hmac
import secrets
import threading
import time

from . import db

SESSION_DAYS = 30
SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
MAX_FAILS_PER_IP = 5
MAX_FAILS_TOTAL = 30
WINDOW_S = 15 * 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(key).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    salt_b64, key_b64 = stored.split("$", 1)
    key = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **SCRYPT)
    return hmac.compare_digest(key, base64.b64decode(key_b64))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
    conn.execute(
        "INSERT INTO sessions(token_hash,expires_at) VALUES(?,?)",
        (_hash_token(token), now + SESSION_DAYS * 86400),
    )
    conn.commit()
    return token


def session_valid(conn, token: str | None) -> bool:
    if not token:
        return False
    row = conn.execute(
        "SELECT expires_at FROM sessions WHERE token_hash=?", (_hash_token(token),)
    ).fetchone()
    return bool(row and row["expires_at"] > time.time())


def delete_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        conn.commit()


class Throttle:
    """Fehlversuche im Speicher zählen: pro IP und (gegen verteilte Angriffe) gesamt."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails: list[tuple[float, str]] = []

    def _prune(self):
        limit = time.time() - WINDOW_S
        self._fails = [f for f in self._fails if f[0] > limit]

    def blocked(self, ip: str) -> bool:
        with self._lock:
            self._prune()
            mine = sum(1 for _, i in self._fails if i == ip)
            return mine >= MAX_FAILS_PER_IP or len(self._fails) >= MAX_FAILS_TOTAL

    def fail(self, ip: str):
        with self._lock:
            self._fails.append((time.time(), ip))

    def clear(self, ip: str):
        with self._lock:
            self._fails = [f for f in self._fails if f[1] != ip]
