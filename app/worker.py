"""Auswertungs-Dienst: arbeitet die Warteschlange mit `claude -p` ab.

Läuft getrennt von der Web-App, weil er die Claude-Anmeldung des Servers braucht.
Claude bekommt hier bewusst keine Tools: Die Eingaben stammen aus Garmin und aus
Notizen, und ein Textauftrag darf auf diesem Weg nichts auf dem Server auslösen.
"""
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

from . import anthropic_key, coach, db, health, trainer, users

log = logging.getLogger("worker")
TIMEOUT_S = 300
HEARTBEAT_S = 60
AUTH_CHECK_S = 3600


class ClaudeFailed(RuntimeError):
    """Claude selbst hat nicht geantwortet, im Gegensatz zu einem Fehler in den Daten."""
POLL_S = 1
API_MODEL = "claude-opus-5-5"


def claude_command(system: str = coach.SYSTEM, api_key: bool = False) -> list[str]:
    command = [
        os.environ.get("TRI_CLAUDE_BIN", "/root/.local/bin/claude"),
        "-p", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
        "--disable-slash-commands", "--no-session-persistence",
        "--system-prompt", system,
    ]
    if api_key:
        command[2:2] = ["--model", API_MODEL]
    return command


def api_key_env(key: str, home: str) -> dict:
    """Leere Umgebung mit eigenem Home: Die Abo-Anmeldung in /root ist unsichtbar, es zählt nur dieser Key."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "TZ": os.environ.get("TZ", "Europe/Berlin"),
            "LANG": "C.UTF-8", "ANTHROPIC_API_KEY": key}


def run_claude(prompt: str, system: str = coach.SYSTEM, api_key: str | None = None) -> str:
    with tempfile.TemporaryDirectory() as cwd:
        env = api_key_env(api_key, cwd) if api_key else None
        done = subprocess.run(claude_command(system, api_key=bool(api_key)), input=prompt, capture_output=True,
                              text=True, timeout=TIMEOUT_S, cwd=cwd, env=env)
    if done.returncode != 0:
        message = (done.stderr or done.stdout or "").strip()[:300]
        raise ClaudeFailed(f"Claude-Aufruf fehlgeschlagen (Code {done.returncode}): {message}")
    text = done.stdout.strip()
    if not text:
        raise ClaudeFailed("Claude hat eine leere Antwort geliefert.")
    return text


def claim(conn):
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM analyses WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    if row:
        conn.execute("UPDATE analyses SET status='running' WHERE id=?", (row["id"],))
    conn.commit()
    return row


def _note_claude(conn, exc: Exception | None) -> None:
    """Merkt sich für die Warnung im Dashboard, ob Claude zuletzt geantwortet hat."""
    if exc is None:
        db.set_setting(conn, "claude_error", "")
    elif isinstance(exc, (ClaudeFailed, subprocess.TimeoutExpired)):
        db.set_setting(conn, "claude_error", _problem(exc))


def _problem(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "Zeitüberschreitung, bitte noch einmal versuchen."
    return str(exc)[:400]


def process(conn, row, runner=run_claude) -> None:
    try:
        result = runner(coach.build_prompt(conn, row["scope"], row["ref"]))
        conn.execute("UPDATE analyses SET status='done',result=?,error=NULL,finished_at=? WHERE id=?",
                     (result, time.time(), row["id"]))
        _note_claude(conn, None)
    except Exception as exc:  # Ein Auftrag darf den Dienst nicht beenden.
        log.exception("Auswertung %s fehlgeschlagen", row["id"])
        _note_claude(conn, exc)
        conn.execute("UPDATE analyses SET status='failed',error=?,finished_at=? WHERE id=?",
                     (_problem(exc), time.time(), row["id"]))
    conn.commit()


def claim_chat(conn):
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM chat_messages WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    if row:
        conn.execute("UPDATE chat_messages SET status='running' WHERE id=?", (row["id"],))
    conn.commit()
    return row


def _ask_trainer(prompt: str) -> str:
    return run_claude(prompt, trainer.SYSTEM)


def claude_key_for(user: users.User) -> str | None:
    """None für Abo-Benutzer. Für alle anderen ihr Key, ohne Key gibt es keinen Aufruf (NoKey)."""
    if not user.own_key:
        return None
    key = anthropic_key.read_key()
    if not key:
        raise NoKey(user.name)
    return key


class NoKey(Exception):
    pass


def runners_for(user: users.User) -> tuple:
    """Aufrufer für Auswertung und Trainer, passend zur Abrechnungsart des Benutzers."""
    key = claude_key_for(user)
    if key is None:
        return run_claude, _ask_trainer
    return (lambda prompt: run_claude(prompt, coach.SYSTEM, api_key=key),
            lambda prompt: run_claude(prompt, trainer.SYSTEM, api_key=key))


def process_chat(conn, row, runner=_ask_trainer) -> None:
    try:
        reply = runner(trainer.build_prompt(conn, row["id"]))
        text, proposal, note = trainer.parse_reply(reply, dt.date.today())
        conn.execute(
            "UPDATE chat_messages SET status='done',content=?,error=NULL,proposal=?,proposal_status=?,proposal_note=? WHERE id=?",
            (text, json.dumps(proposal, ensure_ascii=False) if proposal else None, "open" if proposal else None, note, row["id"]))
        _note_claude(conn, None)
    except Exception as exc:  # Ein Auftrag darf den Dienst nicht beenden.
        log.exception("Chat %s fehlgeschlagen", row["id"])
        _note_claude(conn, exc)
        conn.execute("UPDATE chat_messages SET status='failed',error=? WHERE id=?", (_problem(exc), row["id"]))
    conn.commit()


def _users() -> list[users.User]:
    """Neu angelegte Benutzer kommen ohne Neustart dazu."""
    if not users.path().exists():  # Die Web-App legt die Datei an, nicht root.
        return []
    return [u for u in users.all_users() if (u.dir / "triathlon.db").exists()]


def _loop(name, claimer, handler, which: int) -> None:
    conns: dict = {}
    while True:
        worked = False
        try:
            known = _users()
        except Exception:
            log.exception("%s: Benutzerliste nicht lesbar", name)
            time.sleep(POLL_S * 5)
            continue
        for user in known:
            with db.use(user.dir):
                try:
                    runner = runners_for(user)[which]
                except NoKey:
                    continue  # Aufträge warten, bis der Benutzer seinen Key einträgt.
                conn = conns.get(user.dir) or conns.setdefault(user.dir, db.connect())
                try:
                    row = claimer(conn)
                except Exception:
                    log.exception("%s: Warteschlange von %s nicht lesbar", name, user.name)
                    conn.rollback()
                    continue
                if row:
                    log.info("%s %s für %s", name, row["id"], user.name)
                    handler(conn, row, runner=runner)
                    worked = True
        if not worked:
            time.sleep(POLL_S)


def check_login() -> dict | None:
    binary = os.environ.get("TRI_CLAUDE_BIN", "/root/.local/bin/claude")
    try:
        done = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        log.warning("Claude-Anmeldung konnte nicht geprüft werden")
        return None
    return health.parse_auth_status(done.stdout)


def _watch() -> None:
    """Meldet regelmäßig, dass der Dienst lebt, und prüft stündlich die Claude-Anmeldung."""
    last_check = 0.0
    status = None
    while True:
        if time.time() - last_check > AUTH_CHECK_S:
            status = check_login()
            last_check = time.time()
        try:
            known = _users()
        except Exception:
            log.exception("Benutzerliste nicht lesbar")
            known = []
        for user in known:
            with db.use(user.dir):
                conn = db.connect()
                try:
                    db.set_setting(conn, "worker_heartbeat", str(time.time()))
                    # Der Abo-Status betrifft nur Abo-Benutzer, sonst käme eine falsche Warnung.
                    if not user.own_key and status:
                        db.set_setting(conn, "claude_status", json.dumps(status))
                    elif user.own_key:
                        db.set_setting(conn, "claude_status", "")
                    conn.commit()
                except Exception:
                    log.exception("Statusmeldung für %s fehlgeschlagen", user.name)
                finally:
                    conn.close()
        time.sleep(HEARTBEAT_S)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    for user in _users():
        with db.use(user.dir):
            db.init()
            conn = db.connect()
            # Aufträge, die ein Neustart unterbrochen hat, wieder einreihen.
            conn.execute("UPDATE analyses SET status='pending' WHERE status='running'")
            conn.execute("UPDATE chat_messages SET status='pending' WHERE status='running'")
            conn.commit()
            conn.close()
    log.info("Auswertungs-Dienst bereit")
    # Getrennte Schleifen: Eine laufende Auswertung lässt den Chat nicht warten.
    threading.Thread(target=_loop, args=("Auswertung", claim, process, 0), daemon=True).start()
    threading.Thread(target=_watch, daemon=True).start()
    _loop("Chat", claim_chat, process_chat, 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
