#!/usr/bin/env python3
"""Benutzer verwalten. Läuft als Benutzer triathlon (siehe ./benutzer), damit alle Dateien der Web-App gehören.

  benutzer liste
  benutzer anlegen NAME [ANZEIGENAME]   legt an und zeigt einen einmaligen Startcode
  benutzer zuruecksetzen NAME           neuer Startcode, Passwort ungültig, alle Geräte abgemeldet
  benutzer passwort NAME                Passwort direkt setzen (unsichtbare Eingabe), meldet alle Geräte ab
  benutzer claude NAME abo|api_key      abo: nutzt die Claude-Anmeldung des Servers, api_key: nur eigener Key (Standard)
"""
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import users  # noqa: E402


def _show_code(name: str, code: str) -> None:
    print(f"\nStartcode für {name}: {code}")
    where = os.environ.get("TRI_ORIGIN", "").strip() or "dem Dashboard"
    print(f"Gültig {users.SETUP_DAYS} Tage. Anmelden auf {where} mit Benutzername {name} und diesem Code,")
    print("danach muss ein eigenes Passwort gesetzt werden. Der Code wird nur gehasht gespeichert und nicht wieder angezeigt.")


def main(args: list[str]) -> int:
    os.umask(0o027)
    users.init()
    command = args[0] if args else "liste"
    if command == "liste":
        conn = users.connect()
        for row in conn.execute("SELECT name, display_name, dir, claude, password_hash IS NOT NULL p FROM users ORDER BY created_at"):
            state = "Passwort gesetzt" if row["p"] else "wartet auf ersten Login"
            print(f"{row['name']:<16} {row['display_name']:<20} {state:<24} claude={row['claude']:<8} data/{row['dir']}")
        conn.close()
        return 0
    if len(args) < 2:
        print(__doc__)
        return 1
    name = args[1].strip().lower()
    try:
        if command == "anlegen":
            display = " ".join(args[2:]).strip() or name.capitalize()
            _show_code(name, users.add(name, display))
        elif command == "zuruecksetzen":
            _show_code(name, users.reset(name))
        elif command == "claude":
            if len(args) != 3:
                print(__doc__)
                return 1
            users.set_claude(name, args[2].strip().lower())
            print(f"{name} nutzt jetzt Claude über: {'das Abo des Servers' if args[2].strip().lower() == users.CLAUDE_ABO else 'den eigenen API-Key'}.")
        elif command == "passwort":
            if not sys.stdin.isatty():
                print("Bitte direkt im Terminal ausführen (die Eingabe ist dann unsichtbar).")
                return 1
            first = getpass.getpass(f"Neues Passwort für {name} (mindestens {users.MIN_PASSWORD} Zeichen): ")
            if first != getpass.getpass("Noch einmal: "):
                print("Stimmt nicht überein, nichts geändert.")
                return 1
            users.set_password(name, first)
            print("Passwort gesetzt, alle Geräte abgemeldet.")
        else:
            print(__doc__)
            return 1
    except (ValueError, users.BadPassword) as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
