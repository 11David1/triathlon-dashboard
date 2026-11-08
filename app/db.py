"""SQLite-Zugriff. Web-App und Auswertungs-Dienst teilen sich diese Datei."""
import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("TRI_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS planned (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  sport TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  duration_min INTEGER,
  distance_km REAL,
  intensity TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS planned_date ON planned(date);
CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  start_local TEXT NOT NULL,
  sport TEXT NOT NULL,
  type_key TEXT,
  name TEXT,
  duration_s REAL,
  distance_m REAL,
  avg_hr REAL,
  max_hr REAL,
  avg_power REAL,
  norm_power REAL,
  tss REAL,
  te_aerobic REAL,
  te_anaerobic REAL,
  elevation_m REAL,
  calories REAL,
  avg_speed REAL,
  avg_cadence REAL,
  raw TEXT NOT NULL,
  details TEXT
);
CREATE INDEX IF NOT EXISTS activities_date ON activities(date);
CREATE TABLE IF NOT EXISTS wellness (
  date TEXT PRIMARY KEY,
  sleep_s REAL,
  sleep_score REAL,
  hrv REAL,
  resting_hr REAL,
  body_battery REAL
);
CREATE TABLE IF NOT EXISTS analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  ref TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS analyses_ref ON analyses(scope, ref, id);
CREATE TABLE IF NOT EXISTS complaints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  started_on TEXT NOT NULL,
  resolved_on TEXT,
  cause TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS complaint_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  complaint_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  severity INTEGER,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS complaint_entries_owner ON complaint_entries(complaint_id, id);
CREATE TABLE IF NOT EXISTS calendar_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  all_day INTEGER NOT NULL,
  free INTEGER NOT NULL,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trainer_knowledge (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  error TEXT,
  proposal TEXT,
  proposal_status TEXT,
  proposal_note TEXT,
  created_at REAL NOT NULL
);
"""


WHOOP_COLUMNS = ("whoop_recovery", "whoop_hrv", "whoop_resting_hr", "whoop_spo2", "whoop_skin_temp",
                 "whoop_sleep_s", "whoop_sleep_performance", "whoop_resp_rate", "whoop_strain")


def db_path() -> Path:
    return DATA_DIR / "triathlon.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(activities)")}
        if "note" not in columns:
            conn.execute("ALTER TABLE activities ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        plan_columns = {row["name"] for row in conn.execute("PRAGMA table_info(planned)")}
        if "google_uid" not in plan_columns:
            conn.execute("ALTER TABLE planned ADD COLUMN google_uid TEXT")
            conn.execute("ALTER TABLE planned ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        wellness_columns = {row["name"] for row in conn.execute("PRAGMA table_info(wellness)")}
        for name in WHOOP_COLUMNS:
            if name not in wellness_columns:
                conn.execute(f"ALTER TABLE wellness ADD COLUMN {name} REAL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS planned_google ON planned(google_uid) WHERE google_uid IS NOT NULL")
        conn.commit()
    finally:
        conn.close()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
