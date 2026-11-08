#!/usr/bin/env python3
"""Setzt das Dashboard-Passwort und meldet alle Geräte ab. Eingabe bleibt unsichtbar."""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import auth, db  # noqa: E402

MIN_LENGTH = 12


def main() -> int:
    if not sys.stdin.isatty():
        print("Bitte direkt im Terminal ausführen (die Eingabe ist dann unsichtbar).")
        return 1
    first = getpass.getpass(f"Neues Passwort (mindestens {MIN_LENGTH} Zeichen): ")
    if len(first) < MIN_LENGTH:
        print("Zu kurz, nichts geändert.")
        return 1
    if first != getpass.getpass("Noch einmal: "):
        print("Stimmt nicht überein, nichts geändert.")
        return 1
    db.init()
    conn = db.connect()
    db.set_setting(conn, "password_hash", auth.hash_password(first))
    conn.execute("DELETE FROM sessions")
    conn.commit()
    conn.close()
    print("Passwort gesetzt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
