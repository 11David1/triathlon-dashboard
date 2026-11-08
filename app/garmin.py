"""Garmin Connect Personal API: Key gegen Token tauschen, Daten holen, speichern.

Die API ist laut Garmin nur lesend. Die Feldnamen sind nicht vollständig öffentlich
dokumentiert, daher wird defensiv gelesen und das Original in `raw` aufbewahrt.
"""
import base64
import datetime as dt
import json
import logging
import os
import time

import httpx

from . import db
from .plan import normalize_sport

log = logging.getLogger("garmin")
BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
FIRST_SYNC_DAYS = 180
REGULAR_SYNC_DAYS = 14
WELLNESS_FIRST_DAYS = 60
WELLNESS_DAYS = 7
DETAIL_MAX_AGE_DAYS = 45
DETAILS_PER_SYNC = 20
AUTO_ANALYZE_MAX_AGE_DAYS = 3
AUTO_ANALYZE_PER_SYNC = 5
WELLNESS_VERSION = "2"


class GarminError(Exception):
    pass


class KeyRejected(GarminError):
    pass


def key_path():
    return db.data_dir() / "garmin.key"


def read_key() -> str | None:
    try:
        return key_path().read_text().strip() or None
    except FileNotFoundError:
        return None


def save_key(key: str) -> None:
    path = key_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")


def delete_key() -> None:
    key_path().unlink(missing_ok=True)


def _jwt_exp(token: str) -> float:
    try:
        part = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return time.time() + 3600


class Client:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.http = httpx.Client(base_url=BASE_URL, timeout=30, transport=transport)
        self.token = None
        self.expires = 0.0

    def close(self):
        self.http.close()

    def _refresh(self):
        try:
            r = self.http.post(TOKEN_PATH, headers={"Authorization": f"ApiKey {self.api_key}", "Accept": "*/*"})
        except httpx.HTTPError as exc:
            raise GarminError(f"Garmin nicht erreichbar ({type(exc).__name__})") from exc
        if r.status_code in (400, 401, 403):
            raise KeyRejected("Garmin lehnt den API-Key ab (abgelaufen oder gelöscht?).")
        if r.status_code >= 400:
            raise GarminError(f"Token-Abruf fehlgeschlagen (HTTP {r.status_code})")
        self.token = r.text.strip()
        self.expires = _jwt_exp(self.token)

    def get(self, path: str, params: dict | None = None):
        if not self.token or self.expires - time.time() < 300:
            self._refresh()
        for attempt in (1, 2):
            try:
                r = self.http.get(path, params=params, headers={
                    "Authorization": f"Bearer {self.token}", "Accept": "application/json"})
            except httpx.HTTPError as exc:
                raise GarminError(f"Garmin nicht erreichbar ({type(exc).__name__})") from exc
            if r.status_code == 401 and attempt == 1:
                self._refresh()
                continue
            break
        if r.status_code == 429:
            raise GarminError("Garmin-Ratelimit erreicht (HTTP 429), nächster Versuch beim nächsten Abgleich.")
        if r.status_code >= 400:
            raise GarminError(f"{path}: HTTP {r.status_code}")
        return r.json() if r.content else None

    def verify(self):
        self.get("/userprofile-service/userprofile/settings")


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(row: dict, *names):
    for name in names:
        value = _num(row.get(name))
        if value is not None:
            return value
    return None


def _deep(obj, name):
    """Sucht ein Feld in verschachtelten Antworten."""
    if isinstance(obj, dict):
        if name in obj and obj[name] is not None:
            return obj[name]
        for value in obj.values():
            found = _deep(value, name)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _deep(value, name)
            if found is not None:
                return found
    return None


def normalize_activity(row: dict) -> dict | None:
    activity_id = row.get("activityId") or row.get("id")
    start = str(row.get("startTimeLocal") or row.get("startTimeGMT") or "")
    if not activity_id or len(start) < 10:
        return None
    kind = row.get("activityType")
    type_key = kind.get("typeKey") if isinstance(kind, dict) else kind
    return {
        "id": int(activity_id),
        "date": start[:10],
        "start_local": start.replace("T", " ")[:19],
        "sport": normalize_sport(type_key),
        "type_key": type_key,
        "name": row.get("activityName") or row.get("name"),
        "duration_s": _first(row, "duration", "elapsedDuration", "movingDuration"),
        "distance_m": _first(row, "distance"),
        "avg_hr": _first(row, "averageHR", "avgHR", "averageHeartRate"),
        "max_hr": _first(row, "maxHR", "maxHeartRate"),
        "avg_power": _first(row, "avgPower", "averagePower"),
        "norm_power": _first(row, "normPower", "normalizedPower"),
        "tss": _first(row, "trainingStressScore"),
        "te_aerobic": _first(row, "aerobicTrainingEffect"),
        "te_anaerobic": _first(row, "anaerobicTrainingEffect"),
        "elevation_m": _first(row, "elevationGain"),
        "calories": _first(row, "calories"),
        "avg_speed": _first(row, "averageSpeed", "avgSpeed"),
        "avg_cadence": _first(row, "averageRunningCadenceInStepsPerMinute",
                              "averageBikingCadenceInRevPerMinute",
                              "averageSwimCadenceInStrokesPerMinute", "avgCadence"),
        "raw": json.dumps(row, ensure_ascii=False),
    }


SPLIT_LABELS = {"RWD_RUN": "Laufen", "RWD_WALK": "Gehen", "RWD_STAND": "Stehen", "INTERVAL_ACTIVE": "Aktiv gesamt"}


def laps(details_json: str | None) -> list[dict]:
    """Abschnitte aus den Details. Die Personal API liefert Zusammenfassungen je Typ,
    zum Beispiel Laufen und Gehen, und keine einzelnen Runden."""
    if not details_json:
        return []
    details = json.loads(details_json)
    rows = None
    for name in ("lapDTOs", "laps", "splitSummaries", "splits"):
        found = _deep(details, name)
        if isinstance(found, list) and found and isinstance(found[0], dict):
            rows = found
            break
    out = []
    for row in (rows or [])[:80]:
        kind = row.get("splitType")
        out.append({
            "label": SPLIT_LABELS.get(kind, kind),
            "distance_m": _first(row, "distance", "totalDistance"),
            "duration_s": _first(row, "duration", "totalDuration", "elapsedDuration"),
            "avg_hr": _first(row, "averageHR", "avgHR"),
            "avg_power": _first(row, "averagePower", "avgPower"),
            "avg_speed": _first(row, "averageSpeed", "avgSpeed"),
        })
    return out


def _windows(start: dt.date, end: dt.date, days: int):
    while start <= end:
        last = min(start + dt.timedelta(days=days - 1), end)
        yield start, last
        start = last + dt.timedelta(days=1)


COLUMNS = ("id", "date", "start_local", "sport", "type_key", "name", "duration_s", "distance_m",
           "avg_hr", "max_hr", "avg_power", "norm_power", "tss", "te_aerobic", "te_anaerobic",
           "elevation_m", "calories", "avg_speed", "avg_cadence", "raw")


def _sync_activities(conn, client, start: dt.date, end: dt.date) -> list[dict]:
    new = []
    offset = 0
    while True:
        rows = client.get("/activitylist-service/activities/search/activities", {
            "start": offset, "limit": 100,
            "startDate": start.isoformat(), "endDate": end.isoformat()}) or []
        if isinstance(rows, dict):
            rows = rows.get("results") or rows.get("activities") or []
        for row in rows:
            act = normalize_activity(row)
            if not act:
                continue
            exists = conn.execute("SELECT 1 FROM activities WHERE id=?", (act["id"],)).fetchone()
            if not exists:
                new.append(act)
            updates = ",".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "id")
            conn.execute(
                f"INSERT INTO activities({','.join(COLUMNS)}) VALUES({','.join('?' * len(COLUMNS))}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                [act[c] for c in COLUMNS])
        conn.commit()
        if len(rows) < 100:
            return new
        offset += len(rows)


def _sync_details(conn, client, today: dt.date) -> None:
    since = (today - dt.timedelta(days=DETAIL_MAX_AGE_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT id FROM activities WHERE id>0 AND details IS NULL AND date>=? ORDER BY date DESC LIMIT ?",
        (since, DETAILS_PER_SYNC)).fetchall()
    for row in rows:
        try:
            details = client.get(f"/activity-service/activity/{row['id']}")
        except KeyRejected:
            raise
        except GarminError as exc:
            log.warning("Details %s: %s", row["id"], exc)
            continue
        conn.execute("UPDATE activities SET details=? WHERE id=?",
                     (json.dumps(details, ensure_ascii=False), row["id"]))
        conn.commit()


def _upsert_wellness(conn, day, **values):
    values = {k: v for k, v in values.items() if v is not None}
    if not day or not values:
        return
    day = str(day)[:10]
    conn.execute("INSERT OR IGNORE INTO wellness(date) VALUES(?)", (day,))
    conn.execute(f"UPDATE wellness SET {','.join(k + '=?' for k in values)} WHERE date=?",
                 [*values.values(), day])


def _rows(payload, *names) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for name in names:
            if isinstance(payload.get(name), list):
                return payload[name]
    return []


def _sync_wellness(conn, client, start: dt.date, end: dt.date) -> list[str]:
    """Fehlende Gesundheitsdaten (kein Gerät, keine Freigabe) stoppen den Abgleich nicht."""
    problems = []

    def collect(label, fetch):
        try:
            fetch()
        except KeyRejected:
            raise
        except GarminError as exc:
            problems.append(f"{label}: {exc}")

    def sleep():
        for first, last in _windows(start, end, 31):
            data = client.get("/sleep-service/sleep/dailySleepSummariesByDate",
                              {"startDate": first.isoformat(), "endDate": last.isoformat()})
            for row in _rows(data, "results", "data", "sleepSummaries"):
                score = _deep(row, "sleepScores")
                overall = score.get("overall") if isinstance(score, dict) else None
                value = overall.get("value") if isinstance(overall, dict) else _deep(row, "sleepScore")
                _upsert_wellness(conn, _deep(row, "calendarDate"),
                                 sleep_s=_num(_deep(row, "sleepTimeSeconds")), sleep_score=_num(value))

    def hrv():
        for first, last in _windows(start, end, 28):
            data = client.get(f"/hrv-service/hrv/daily/{first}/{last}")
            for row in _rows(data, "hrvSummaries", "results", "data"):
                _upsert_wellness(conn, row.get("calendarDate"), hrv=_num(row.get("lastNightAvg")))

    def daily():
        data = client.get("/usersummary-service/v2/dailysummaries",
                          {"startDate": start.isoformat(), "endDate": end.isoformat()})
        for row in _rows(data, "data", "results"):
            heart = row.get("heartRate") if isinstance(row.get("heartRate"), dict) else {}
            battery = row.get("bodyBattery") if isinstance(row.get("bodyBattery"), dict) else {}
            _upsert_wellness(conn, row.get("calendarDate"),
                             resting_hr=_first(heart, "restingValue"),
                             body_battery=_first(battery, "maxValue"))

    collect("Schlaf", sleep)
    collect("HRV", hrv)
    collect("Tageswerte", daily)
    conn.commit()
    return problems


def queue_analysis(conn, scope: str, ref: str) -> int:
    row = conn.execute(
        "SELECT id FROM analyses WHERE scope=? AND ref=? AND status IN ('pending','running')",
        (scope, ref)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO analyses(scope,ref,status,created_at) VALUES(?,?,'pending',?)",
                       (scope, ref, time.time()))
    conn.commit()
    return cur.lastrowid


def sync(conn, client: Client, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    first = db.get_setting(conn, "initial_sync_done") != "1"
    start = today - dt.timedelta(days=FIRST_SYNC_DAYS if first else REGULAR_SYNC_DAYS)
    new = _sync_activities(conn, client, start, today)
    _sync_details(conn, client, today)
    # Eine neue Schema-Version lädt die Erholungsdaten einmal rückwirkend nach.
    backfill = first or db.get_setting(conn, "wellness_version") != WELLNESS_VERSION
    well_start = today - dt.timedelta(days=WELLNESS_FIRST_DAYS if backfill else WELLNESS_DAYS)
    problems = _sync_wellness(conn, client, well_start, today)
    # Nur frische Einheiten automatisch auswerten, nicht die ganze Historie.
    cutoff = (today - dt.timedelta(days=AUTO_ANALYZE_MAX_AGE_DAYS)).isoformat()
    fresh = sorted((a for a in new if a["date"] >= cutoff), key=lambda a: a["start_local"])
    for act in fresh[-AUTO_ANALYZE_PER_SYNC:]:
        queue_analysis(conn, "activity", str(act["id"]))
    db.set_setting(conn, "initial_sync_done", "1")
    db.set_setting(conn, "wellness_version", WELLNESS_VERSION)
    conn.commit()
    return {"new": len(new), "problems": problems}


def run_sync(transport=None) -> dict:
    """Ein kompletter Abgleich; Status landet in den Settings, nie der Key."""
    key = read_key()
    conn = db.connect()
    try:
        if not key:
            return {"new": 0, "problems": ["Kein API-Key hinterlegt."]}
        client = Client(key, transport)
        try:
            result = sync(conn, client)
            # Tokens gelten 24 Stunden, außer der Key läuft vorher ab: das letzte Sicherheitsnetz.
            ends_soon = client.expires and client.expires - time.time() < 23 * 3600
            hint = dt.date.fromtimestamp(client.expires).isoformat() if ends_soon else ""
            db.set_setting(conn, "garmin_key_expiry_hint", hint)
            db.set_setting(conn, "last_sync_at", str(time.time()))
            db.set_setting(conn, "last_sync_error", "; ".join(result["problems"]))
        except GarminError as exc:
            db.set_setting(conn, "last_sync_error", str(exc))
            result = {"new": 0, "problems": [str(exc)]}
        finally:
            client.close()
        conn.commit()
        return result
    finally:
        conn.close()
