"""Nächtliche Sicherung der Datenbank. Der Garmin-Key bleibt bewusst außen vor."""
import datetime as dt
import gzip
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from . import db

log = logging.getLogger("backup")
KEEP = 30
NAME = re.compile(r"^triathlon-\d{4}-\d{2}-\d{2}_\d{6}\.db\.gz$")


def backup_dir() -> Path:
    return Path(os.environ.get("TRI_BACKUP_DIR", "/var/backups/triathlon-dashboard"))


def list_backups() -> list[Path]:
    """Sicherungen, die neueste zuerst. Fremde Dateien im Ordner werden nie angefasst."""
    try:
        return sorted((p for p in backup_dir().iterdir() if NAME.match(p.name)), key=lambda p: p.name, reverse=True)
    except FileNotFoundError:
        return []


def create(now: dt.datetime | None = None) -> Path:
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"triathlon-{(now or dt.datetime.now()):%Y-%m-%d_%H%M%S}.db.gz"
    raw = target_dir / f".{name}.raw"
    packed = target_dir / f".{name}.tmp"
    try:
        source = sqlite3.connect(db.db_path(), timeout=30)
        copy = sqlite3.connect(raw)
        try:
            # Die Backup-API liefert auch während laufender Schreibzugriffe einen stimmigen Stand.
            source.backup(copy)
            result = copy.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"Prüfung der Sicherung fehlgeschlagen: {result}")
        finally:
            copy.close()
            source.close()
        descriptor = os.open(packed, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as out, gzip.GzipFile(fileobj=out, mode="wb") as zipped, open(raw, "rb") as data:
            shutil.copyfileobj(data, zipped)
        with open(packed, "rb") as check:
            os.fsync(check.fileno())
        final = target_dir / name
        os.replace(packed, final)
        return final
    finally:
        raw.unlink(missing_ok=True)
        packed.unlink(missing_ok=True)


def prune(keep: int = KEEP) -> int:
    old = list_backups()[keep:]
    for path in old:
        path.unlink()
    return len(old)


def run() -> int:
    conn = db.connect()
    try:
        try:
            path = create()
            removed = prune()
            db.set_setting(conn, "last_backup_at", str(time.time()))
            db.set_setting(conn, "backup_error", "")
            log.info("Gesichert: %s (%d Bytes), %d alte entfernt", path.name, path.stat().st_size, removed)
            return 0
        except Exception as exc:  # Der Fehler muss im Dashboard ankommen, nicht nur im Log.
            log.exception("Sicherung fehlgeschlagen")
            db.set_setting(conn, "backup_error", str(exc)[:300])
            return 1
    finally:
        conn.commit()
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    raise SystemExit(run())
