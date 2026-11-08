"""Benutzer und Sitzungen. Liegt getrennt von den Trainingsdaten in data/users.db.

Neue Benutzer bekommen einen einmaligen Startcode (nur gehasht gespeichert). Beim ersten Login
mit Benutzername und Startcode muss ein eigenes Passwort gesetzt werden, danach gilt nur noch das.
Die Daten jedes Benutzers liegen in einem eigenen Ordner mit eigener Datenbank (data/users/<name>/).
Nur wer von der früheren Einzelbenutzer-Version übernommen wurde, behält den Ordner data/ selbst,
damit Sicherung und Wiederherstellung unverändert bleiben.
"""
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import auth, db

NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
MIN_PASSWORD = 12
SETUP_DAYS = 7
# Für unbekannte Namen trotzdem rechnen, damit die Antwortzeit nichts verrät.
_DUMMY_HASH = auth.hash_password(secrets.token_urlsafe(16))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  name TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  dir TEXT NOT NULL UNIQUE,
  password_hash TEXT,
  setup_hash TEXT,
  setup_expires REAL,
  created_at REAL NOT NULL,
  claude TEXT NOT NULL DEFAULT 'api_key'
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user TEXT NOT NULL,
  expires_at REAL NOT NULL
);
"""


# Wie Trainer und Auswertung Claude aufrufen: "abo" = Claude-Anmeldung des Servers (das Abo des Betreibers),
# "api_key" = nur mit dem eigenen API-Key des Benutzers, nie über das Abo. Neue Benutzer bekommen "api_key",
# das Abo gibt der Betreiber ausdrücklich frei (benutzer claude NAME abo).
CLAUDE_ABO = "abo"
CLAUDE_API_KEY = "api_key"
# Name für den Benutzer, der beim Umstieg von der Einzelbenutzer-Version entsteht.
LEGACY_USER = "admin"


@dataclass(frozen=True)
class User:
    name: str
    display_name: str
    dir: Path
    claude: str = CLAUDE_API_KEY

    @property
    def own_key(self) -> bool:
        return self.claude != CLAUDE_ABO


class SetupRequired(Exception):
    """Startcode stimmt, aber es fehlt noch ein eigenes Passwort."""


class BadPassword(ValueError):
    pass


def path() -> Path:
    return db.ROOT_DIR / "users.db"


def connect() -> sqlite3.Connection:
    return db.open_db(path())


def _user(row) -> User:
    return User(row["name"], row["display_name"], (db.ROOT_DIR / row["dir"]).resolve(), row["claude"])


def init() -> None:
    """Legt die Tabelle an und übernimmt beim ersten Start ein Einzel-Passwort der alten Version als Benutzer admin."""
    db.ROOT_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if "claude" not in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}:
            # Bis hierhin lief alles über das Abo, das bleibt nur für den übernommenen Benutzer (Ordner data/) so.
            conn.execute("ALTER TABLE users ADD COLUMN claude TEXT NOT NULL DEFAULT 'api_key'")
            conn.execute("UPDATE users SET claude='abo' WHERE dir='.'")
        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
            _migrate_single_user(conn)
        conn.commit()
    finally:
        conn.close()
    for user in all_users():
        with db.use(user.dir):
            db.init()


def _migrate_single_user(conn) -> None:
    old = db.ROOT_DIR / "triathlon.db"
    if not old.exists():
        return
    legacy = db.open_db(old)
    try:
        row = legacy.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
        has_sessions = legacy.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()
        sessions = list(legacy.execute("SELECT token_hash, expires_at FROM sessions")) if has_sessions else []
    finally:
        legacy.close()
    conn.execute("INSERT INTO users(name,display_name,dir,password_hash,created_at,claude) VALUES(?,?,'.',?,?,'abo')",
                 (LEGACY_USER, LEGACY_USER.capitalize(), row["value"] if row else None, time.time()))
    # Angemeldete Geräte bleiben angemeldet.
    conn.executemany("INSERT OR IGNORE INTO sessions(token_hash,user,expires_at) VALUES(?,?,?)",
                     [(s["token_hash"], LEGACY_USER, s["expires_at"]) for s in sessions])


def all_users() -> list[User]:
    conn = connect()
    try:
        return [_user(r) for r in conn.execute("SELECT * FROM users ORDER BY created_at")]
    finally:
        conn.close()


def get(name: str) -> User | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        return _user(row) if row else None
    finally:
        conn.close()


def _new_code() -> str:
    # 4 Gruppen à 4 Zeichen, ohne leicht verwechselbare Zeichen.
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


def add(name: str, display_name: str) -> str:
    """Legt einen Benutzer mit eigenem Datenordner an und gibt den Startcode zurück."""
    if not NAME.match(name):
        raise ValueError("Benutzername: 2 bis 32 Zeichen, Kleinbuchstaben, Ziffern, - oder _, beginnt mit einem Buchstaben.")
    code = _new_code()
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
            raise ValueError(f"Benutzer {name} gibt es schon.")
        relative = f"users/{name}"
        conn.execute(
            "INSERT INTO users(name,display_name,dir,setup_hash,setup_expires,created_at) VALUES(?,?,?,?,?,?)",
            (name, display_name, relative, auth.hash_password(code), time.time() + SETUP_DAYS * 86400, time.time()))
        user_dir = db.ROOT_DIR / relative
        user_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        with db.use(user_dir):
            db.init()
            data = db.connect()
            db.set_setting(data, "athlete_name", display_name)
            data.commit()
            data.close()
        conn.commit()
    finally:
        conn.close()
    return code


def set_claude(name: str, mode: str) -> None:
    """Legt fest, ob der Benutzer das Claude-Abo des Servers nutzen darf oder einen eigenen API-Key braucht."""
    if mode not in (CLAUDE_ABO, CLAUDE_API_KEY):
        raise ValueError(f"Claude-Modus: {CLAUDE_ABO} oder {CLAUDE_API_KEY}.")
    conn = connect()
    try:
        if not conn.execute("UPDATE users SET claude=? WHERE name=?", (mode, name)).rowcount:
            raise ValueError(f"Benutzer {name} gibt es nicht.")
        conn.commit()
    finally:
        conn.close()


def reset(name: str) -> str:
    """Neuer Startcode, altes Passwort ungültig, alle Geräte abgemeldet."""
    code = _new_code()
    conn = connect()
    try:
        done = conn.execute("UPDATE users SET password_hash=NULL, setup_hash=?, setup_expires=? WHERE name=?",
                            (auth.hash_password(code), time.time() + SETUP_DAYS * 86400, name))
        if not done.rowcount:
            raise ValueError(f"Benutzer {name} gibt es nicht.")
        conn.execute("DELETE FROM sessions WHERE user=?", (name,))
        conn.commit()
    finally:
        conn.close()
    return code


def set_password(name: str, password: str) -> None:
    """Setzt das Passwort (scrypt-Hash) und meldet alle Geräte ab."""
    if len(password) < MIN_PASSWORD:
        raise BadPassword(f"Das Passwort braucht mindestens {MIN_PASSWORD} Zeichen.")
    conn = connect()
    try:
        done = conn.execute("UPDATE users SET password_hash=?, setup_hash=NULL, setup_expires=NULL WHERE name=?",
                            (auth.hash_password(password), name))
        if not done.rowcount:
            raise ValueError(f"Benutzer {name} gibt es nicht.")
        conn.execute("DELETE FROM sessions WHERE user=?", (name,))
        conn.commit()
    finally:
        conn.close()


def check_login(name: str, password: str, new_password: str | None = None) -> User | None:
    """Gibt den Benutzer zurück, wenn die Anmeldung gilt.

    Beim ersten Login ist `password` der Startcode. Ohne `new_password` kommt dann SetupRequired,
    mit gültigem `new_password` wird es gesetzt und der Startcode verfällt.
    """
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE name=?", (name.strip().lower(),)).fetchone()
    finally:
        conn.close()
    if row is None:
        auth.verify_password(password, _DUMMY_HASH)
        return None
    if row["password_hash"]:
        return _user(row) if auth.verify_password(password, row["password_hash"]) else None
    if not row["setup_hash"] or (row["setup_expires"] or 0) < time.time():
        auth.verify_password(password, _DUMMY_HASH)
        return None
    if not auth.verify_password(password.strip().lower(), row["setup_hash"]):
        return None
    if not new_password:
        raise SetupRequired()
    if new_password.strip().lower() == password.strip().lower():
        raise BadPassword("Bitte ein eigenes Passwort wählen, nicht den Startcode.")
    set_password(row["name"], new_password)
    return _user(row)


def create_session(user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn = connect()
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
        conn.execute("INSERT INTO sessions(token_hash,user,expires_at) VALUES(?,?,?)",
                     (auth.hash_token(token), user.name, now + auth.SESSION_DAYS * 86400))
        conn.commit()
    finally:
        conn.close()
    return token


def session_user(token: str | None) -> User | None:
    if not token:
        return None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.name=s.user WHERE s.token_hash=? AND s.expires_at>?",
            (auth.hash_token(token), time.time())).fetchone()
        return _user(row) if row else None
    finally:
        conn.close()


def delete_session(token: str | None) -> None:
    if not token:
        return
    conn = connect()
    try:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (auth.hash_token(token),))
        conn.commit()
    finally:
        conn.close()
