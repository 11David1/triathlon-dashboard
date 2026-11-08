"""Warnungen für das Dashboard: Was müsste der Athlet wissen, damit nichts still stehen bleibt?"""
import datetime as dt
import json
import time

from . import db, garmin, whoop

KEY_WARN_DAYS = 14
LOGIN_WARN_DAYS = 3
SYNC_STALE_S = 2 * 3600
WORKER_STALE_S = 5 * 60
BACKUP_STALE_S = 48 * 3600


def parse_auth_status(text: str) -> dict | None:
    """Liest aus `claude auth status` nur Anmeldestatus und Ablauf, keine Kontodaten."""
    try:
        data = json.loads(text)
        return {"logged_in": bool(data["loggedIn"]), "expires_at": data.get("loginExpiresAt")}
    except (ValueError, KeyError, TypeError):
        return None


def _de(date: dt.date) -> str:
    return date.strftime("%d.%m.%Y")


def _key_alerts(conn, today: dt.date) -> list[dict]:
    expiry = db.get_setting(conn, "garmin_key_expires") or db.get_setting(conn, "garmin_key_expiry_hint")
    if not expiry:
        return [{"level": "info", "text": "Trag in den Einstellungen ein, wann dein Garmin-Key abläuft, dann warne ich dich rechtzeitig.",
                 "action": "settings"}]
    date = dt.date.fromisoformat(expiry)
    days = (date - today).days
    if days < 0:
        return [{"level": "problem", "text": f"Dein Garmin-Key ist am {_de(date)} abgelaufen. Trag einen neuen ein, sonst kommen keine Trainings mehr an.",
                 "action": "settings"}]
    if days <= KEY_WARN_DAYS:
        when = "heute" if days == 0 else "morgen" if days == 1 else f"in {days} Tagen"
        return [{"level": "warning", "text": f"Dein Garmin-Key läuft {when} ab ({_de(date)}). Erstelle rechtzeitig einen neuen.", "action": "settings"}]
    return []


def _sync_alerts(conn, now: float) -> list[dict]:
    error = db.get_setting(conn, "last_sync_error", "")
    if error:
        return [{"level": "problem", "text": f"Garmin-Abgleich: {error}"}]
    last = db.get_setting(conn, "last_sync_at")
    if last and now - float(last) > SYNC_STALE_S:
        hours = round((now - float(last)) / 3600)
        return [{"level": "warning", "text": f"Der letzte Garmin-Abgleich ist {hours} Stunden her, eigentlich läuft er alle 30 Minuten."}]
    return []


def _claude_alerts(conn, now: float, today: dt.date) -> list[dict]:
    alerts = []
    beat = db.get_setting(conn, "worker_heartbeat")
    if not beat or now - float(beat) > WORKER_STALE_S:
        alerts.append({"level": "problem", "text": "Der Dienst für Auswertungen und Trainer läuft gerade nicht. Neue Anfragen bleiben liegen."})
    raw = db.get_setting(conn, "claude_status")
    status = json.loads(raw) if raw else None
    if status and not status["logged_in"]:
        alerts.append({"level": "problem", "text": "Claude ist auf dem Server abgemeldet. Führe im Terminal „claude auth login“ aus, sonst antworten Trainer und Auswertung nicht."})
    elif status and status.get("expires_at"):
        date = dt.datetime.fromisoformat(status["expires_at"].replace("Z", "+00:00")).date()
        if (date - today).days <= LOGIN_WARN_DAYS:
            alerts.append({"level": "warning", "text": f"Die Claude-Anmeldung auf dem Server läuft am {_de(date)} ab. Führe im Terminal „claude auth login“ aus, falls sie sich nicht von selbst verlängert."})
    error = db.get_setting(conn, "claude_error")
    if error:
        alerts.append({"level": "problem", "text": f"Claude hat zuletzt nicht geantwortet: {error}"})
    return alerts


def _calendar_alerts(conn) -> list[dict]:
    error = db.get_setting(conn, "calendar_error", "")
    if error:
        return [{"level": "warning", "text": f"Google Kalender: {error} Der Trainer plant so lange mit dem letzten bekannten Stand.", "action": "settings"}]
    return []


def _whoop_alerts(conn, now: float) -> list[dict]:
    if not whoop.connected():
        return []
    error = db.get_setting(conn, "whoop_error", "")
    if error:
        return [{"level": "warning", "text": f"WHOOP-Abgleich: {error}", "action": "settings"}]
    last = db.get_setting(conn, "whoop_last_sync_at")
    if last and now - float(last) > SYNC_STALE_S:
        hours = round((now - float(last)) / 3600)
        return [{"level": "warning", "text": f"Der letzte WHOOP-Abgleich ist {hours} Stunden her, eigentlich läuft er alle 30 Minuten."}]
    return []


def _backup_alerts(conn, now: float) -> list[dict]:
    error = db.get_setting(conn, "backup_error", "")
    if error:
        return [{"level": "problem", "text": f"Die letzte Sicherung ist fehlgeschlagen: {error}"}]
    last = db.get_setting(conn, "last_backup_at")
    if last and now - float(last) > BACKUP_STALE_S:
        days = round((now - float(last)) / 86400)
        return [{"level": "warning", "text": f"Die letzte Sicherung ist {days} Tage her, eigentlich läuft sie jede Nacht."}]
    return []


def alerts(conn, now: float | None = None, today: dt.date | None = None) -> list[dict]:
    now = now or time.time()
    today = today or dt.date.today()
    found = [*_claude_alerts(conn, now, today), *_calendar_alerts(conn), *_whoop_alerts(conn, now), *_backup_alerts(conn, now)]
    if garmin.read_key() is None:
        return [{"level": "info", "text": "Verbinde Garmin, damit deine Trainings hier erscheinen.", "action": "settings"}, *found]
    return [*_key_alerts(conn, today), *_sync_alerts(conn, now), *found]
