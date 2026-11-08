import datetime as dt
import gzip
import sqlite3
import stat

import pytest

from app import backup, db, health


@pytest.fixture
def folder(anon, tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_BACKUP_DIR", str(tmp_path / "b"))
    return tmp_path / "b"


def restore(path, target):
    target.write_bytes(gzip.decompress(path.read_bytes()))
    return sqlite3.connect(target)


def test_backup_can_be_restored_including_latest_writes(client, folder, tmp_path):
    client.post("/api/planned", json={"date": "2026-12-01", "sport": "run", "title": "Muss rein"})
    client.post("/api/complaints", json={"name": "Knie", "started_on": "2026-11-01", "text": "zieht"})
    assert backup.run() == 0
    files = backup.list_backups()
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    restored = restore(files[0], tmp_path / "r.db")
    assert restored.execute("SELECT title FROM planned").fetchone()[0] == "Muss rein"
    assert restored.execute("SELECT text FROM complaint_entries").fetchone()[0] == "zieht"
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored.close()
    assert client.get("/api/settings").json()["backup_count"] == 1
    assert client.get("/api/settings").json()["last_backup_at"] is not None


def test_keeps_newest_and_leaves_foreign_files_alone(anon, folder):
    start = dt.datetime(2026, 1, 1, 3, 30)
    for day in range(35):
        backup.create(start + dt.timedelta(days=day))
    (folder / "wichtig.txt").write_text("bleibt")
    assert backup.prune() == 5
    names = [p.name for p in backup.list_backups()]
    assert len(names) == 30
    assert names[0].startswith("triathlon-2026-02-04") and names[-1].startswith("triathlon-2026-01-06")
    assert (folder / "wichtig.txt").exists()


def test_no_temp_files_left_behind(anon, folder):
    backup.create()
    assert [p.name for p in folder.iterdir() if p.name.startswith(".")] == []


def test_failure_is_reported_and_old_backups_survive(anon, folder, monkeypatch):
    backup.create(dt.datetime(2026, 1, 1))

    def broken(*_):
        raise OSError("Platte voll")

    monkeypatch.setattr(backup, "create", broken)
    assert backup.run() == 1
    assert len(backup.list_backups()) == 1
    conn = db.connect()
    assert "Platte voll" in health.alerts(conn)[-1]["text"]
    conn.close()


def test_success_clears_error(anon, folder):
    conn = db.connect()
    db.set_setting(conn, "backup_error", "alt")
    conn.commit()
    assert backup.run() == 0
    assert db.get_setting(conn, "backup_error") == ""
    assert not any("Sicherung" in a["text"] for a in health.alerts(conn))
    conn.close()


def test_stale_backup_warns(anon):
    conn = db.connect()
    db.set_setting(conn, "last_backup_at", "1000000")
    db.set_setting(conn, "worker_heartbeat", "1000000")
    now = 1000000 + 3 * 86400
    assert any("3 Tage her" in a["text"] for a in health.alerts(conn, now=now))
    assert not any("Sicherung" in a["text"] for a in health.alerts(conn, now=1000000 + 3600))
    conn.close()


def test_key_is_not_in_backup(client, fake, folder, tmp_path):
    client.post("/api/settings/garmin-key", json={"key": fake.good_key})
    backup.run()
    assert fake.good_key.encode() not in gzip.decompress(backup.list_backups()[0].read_bytes())
