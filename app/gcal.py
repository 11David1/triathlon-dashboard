"""Google Kalender über die geheime iCal-Adresse: nur lesen, nur Titel und Zeiten.

Die Adressen sind Geheimnisse. Sie liegen wie der Garmin-Key in einer Datei, die nur
der Dienst lesen darf, und tauchen weder in Antworten noch in Fehlermeldungen auf.
"""
import datetime as dt
import logging
import os
import re
import threading
import time
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from . import db
from .plan import classify_title

log = logging.getLogger("gcal")
LOCAL = ZoneInfo("Europe/Berlin")
URL = re.compile(r"^https://(calendar|www)\.google\.com/calendar/ical/[^/\s]+/(private-[0-9a-zA-Z]+|public)/basic\.ics$")
MAX_URLS = 5
MAX_BYTES = 30 * 1024 * 1024
HORIZON_DAYS = 60
PAST_DAYS = 180
FRESH_S = 300
_lock = threading.Lock()


class CalendarError(Exception):
    pass


def urls_path():
    return db.DATA_DIR / "calendar.urls"


def read_urls() -> list[str]:
    try:
        return [line.strip() for line in urls_path().read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def save_urls(urls: list[str]) -> None:
    descriptor = os.open(urls_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(urls) + "\n")


def delete_urls() -> None:
    urls_path().unlink(missing_ok=True)


def valid_url(url: str) -> bool:
    """Nur Googles iCal-Adressen: Der Server soll keine beliebigen Adressen abrufen."""
    return bool(URL.match(url))


def normalize_url(text: str) -> str:
    """Nimmt Eingaben so, wie sie beim Kopieren oft ankommen: mit Anführungszeichen oder als webcal-Link."""
    url = text.strip().strip("\"'<>").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def explain_bad_url(url: str) -> str:
    """Sagt ohne die Adresse zu wiederholen, was stattdessen kopiert wurde."""
    lower = url.lower()
    if "<iframe" in lower or "/calendar/embed" in lower:
        return "Das ist die Adresse zum Einbetten, nicht die iCal-Adresse. Kopiere weiter unten „Geheime Adresse im iCal-Format“."
    if "cid=" in lower:
        return "Das ist der Link zum Teilen des Kalenders. Du brauchst stattdessen „Geheime Adresse im iCal-Format“ unter „Kalender integrieren“."
    if "calendar.google.com/calendar/" in lower and "/ical/" not in lower:
        return "Das ist die Adresse der Kalender-Seite im Browser. Du brauchst „Geheime Adresse im iCal-Format“ aus den Einstellungen des Kalenders, unter „Kalender integrieren“."
    if "/ical/" in lower and not lower.endswith("basic.ics"):
        return "Die Adresse ist unvollständig: Sie muss auf basic.ics enden. Am besten mit dem Kopieren-Symbol neben dem Feld kopieren."
    if not lower.startswith(("http://", "https://")):
        return "Das sieht nicht wie eine Adresse aus. Sie beginnt mit https://calendar.google.com/calendar/ical/ und endet auf basic.ics. Falls du nur Punkte siehst: mit dem Kopieren-Symbol neben dem Feld kopieren."
    return "Das ist keine iCal-Adresse von Google Kalender. Sie beginnt mit https://calendar.google.com/calendar/ical/ und endet auf basic.ics."


def _fetch(url: str, transport=None) -> bytes:
    try:
        with httpx.Client(timeout=20, transport=transport, follow_redirects=False) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise CalendarError(f"Google Kalender nicht erreichbar ({type(exc).__name__}).") from None
    if response.status_code in (401, 403, 404):
        raise CalendarError("Google lehnt die Kalender-Adresse ab. Vielleicht wurde sie zurückgesetzt.")
    if response.status_code != 200:
        raise CalendarError(f"Google Kalender antwortet mit Status {response.status_code}.")
    if len(response.content) > MAX_BYTES:
        raise CalendarError("Der Kalender ist zu groß.")
    return response.content


def _local_time(value) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL)
    return value.astimezone(LOCAL)


def parse_events(data: bytes, today: dt.date, days: int = HORIZON_DAYS) -> list[dict]:
    try:
        calendar = icalendar.Calendar.from_ical(data)
        found = recurring_ical_events.of(calendar).between(today, today + dt.timedelta(days=days))
    except Exception:
        raise CalendarError("Die Kalender-Daten ließen sich nicht lesen.") from None
    events = []
    for event in found:
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start = event.decoded("DTSTART")
        end = event.decoded("DTEND") if event.get("DTEND") else None
        title = str(event.get("SUMMARY") or "Termin")[:200]
        uid = str(event.get("UID") or title)
        free = str(event.get("TRANSP", "")).upper() == "TRANSPARENT"
        if isinstance(start, dt.datetime):
            begin = _local_time(start)
            finish = _local_time(end) if isinstance(end, dt.datetime) else begin
            events.append({"start": begin.strftime("%Y-%m-%dT%H:%M"), "end": finish.strftime("%Y-%m-%dT%H:%M"),
                           "all_day": 0, "free": int(free), "title": title, "uid": f"{uid}|{begin:%Y-%m-%dT%H:%M}"})
        else:
            # Bei ganztägigen Terminen ist das Ende in iCal der Tag danach.
            last = (end - dt.timedelta(days=1)) if isinstance(end, dt.date) and end > start else start
            events.append({"start": start.isoformat(), "end": last.isoformat(), "all_day": 1, "free": int(free), "title": title, "uid": f"{uid}|{start.isoformat()}"})
    events.sort(key=lambda item: item["start"])
    return events


def load_all(urls: list[str], today: dt.date, transport=None) -> list[dict]:
    events = []
    start = today - dt.timedelta(days=PAST_DAYS)
    for url in urls:
        events.extend(parse_events(_fetch(url, transport), start, PAST_DAYS + HORIZON_DAYS))
    events.sort(key=lambda item: item["start"])
    return events


def terms(conn, key: str) -> list[str]:
    return [line.strip() for line in db.get_setting(conn, key, "").splitlines() if line.strip()]


def _plan_note(event: dict) -> str:
    if event["all_day"]:
        return "Aus Google Kalender, ganztägig"
    return f"Aus Google Kalender, {event['start'][11:]} bis {event['end'][11:]} Uhr"


def _minutes(event: dict) -> int | None:
    if event["all_day"]:
        return None
    start = dt.datetime.fromisoformat(event["start"])
    end = dt.datetime.fromisoformat(event["end"])
    minutes = round((end - start).total_seconds() / 60)
    return minutes if 0 < minutes <= 1440 else None


def store(conn, events: list[dict], today: dt.date | None = None) -> dict:
    """Trainings werden zu Plan-Einträgen, alles andere bleibt ein Termin nur für den Trainer."""
    today = today or dt.date.today()
    extra = terms(conn, "calendar_training_terms")
    ignore = terms(conn, "calendar_ignore_terms")
    own = {(row["date"], row["sport"]) for row in conn.execute("SELECT date,sport FROM planned WHERE google_uid IS NULL")}
    others = []
    seen = set()
    first_day = today.isoformat()
    trainings = 0
    for event in events:
        sport = classify_title(event["title"], extra, ignore)
        if sport is None:
            # Vergangene private Termine braucht niemand, sie werden gar nicht erst gespeichert.
            if event["end"][:10] >= first_day:
                others.append(event)
            continue
        date = event["start"][:10]
        # Steht dieselbe Sportart am Tag schon im Dashboard-Plan, zählt sie nicht doppelt.
        if (date, sport) in own:
            continue
        uid = event.get("uid") or f"{event['title']}|{event['start']}"
        seen.add(uid)
        trainings += 1
        conn.execute(
            "INSERT INTO planned(date,sport,title,duration_min,notes,google_uid) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(google_uid) WHERE google_uid IS NOT NULL DO UPDATE SET date=excluded.date,sport=excluded.sport,"
            "title=excluded.title,duration_min=excluded.duration_min,notes=excluded.notes",
            (date, sport, event["title"][:120], _minutes(event), _plan_note(event), uid))
    # Was in Google gelöscht oder umbenannt wurde, verschwindet auch hier.
    window = (today - dt.timedelta(days=PAST_DAYS)).isoformat()
    for row in conn.execute("SELECT id,google_uid FROM planned WHERE google_uid IS NOT NULL AND date>=?", (window,)).fetchall():
        if row["google_uid"] not in seen:
            conn.execute("DELETE FROM planned WHERE id=?", (row["id"],))
    conn.execute("DELETE FROM calendar_events")
    conn.executemany("INSERT INTO calendar_events(start,end,all_day,free,title) VALUES(:start,:end,:all_day,:free,:title)", others)
    db.set_setting(conn, "calendar_last_fetch", str(time.time()))
    db.set_setting(conn, "calendar_error", "")
    return {"trainings": trainings, "others": len(others)}


def refresh(transport=None) -> None:
    """Aktualisiert die Termine. Bei einem Fehler bleibt der letzte Stand erhalten."""
    urls = read_urls()
    if not urls:
        return
    with _lock:
        conn = db.connect()
        try:
            try:
                store(conn, load_all(urls, dt.date.today(), transport))
            except CalendarError as exc:
                db.set_setting(conn, "calendar_error", str(exc))
            conn.commit()
        finally:
            conn.close()


def refresh_if_stale(transport=None) -> None:
    if not read_urls():
        return
    conn = db.connect()
    try:
        last = float(db.get_setting(conn, "calendar_last_fetch") or 0)
    finally:
        conn.close()
    if time.time() - last > FRESH_S:
        refresh(transport)


def disconnect() -> None:
    delete_urls()
    conn = db.connect()
    try:
        conn.execute("DELETE FROM calendar_events")
        conn.execute("DELETE FROM planned WHERE google_uid IS NOT NULL")
        db.set_setting(conn, "calendar_error", "")
        db.set_setting(conn, "calendar_last_fetch", "")
        conn.commit()
    finally:
        conn.close()


def trainer_context(conn, today: dt.date, days: int = 22) -> dict:
    """Termine für den Trainer. Sagt ehrlich, wenn kein oder nur ein alter Stand vorliegt."""
    last = db.get_setting(conn, "calendar_last_fetch")
    if not last:
        return {"status": "nicht verbunden, du kennst keine Termine"}
    until = (today + dt.timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT * FROM calendar_events WHERE start<? AND end>=? ORDER BY start",
                        (until, today.isoformat())).fetchall()
    events = []
    for row in rows:
        item = {"titel": row["title"]}
        if row["all_day"]:
            item.update({"datum": row["start"], "ganztaegig": True})
            if row["end"] != row["start"]:
                item["bis"] = row["end"]
        else:
            item.update({"datum": row["start"][:10], "von": row["start"][11:], "bis": row["end"][11:]})
            if row["end"][:10] != row["start"][:10]:
                item["bis_datum"] = row["end"][:10]
        if row["free"]:
            item["als_frei_markiert"] = True
        events.append(item)
    out = {"status": "aktuell", "termine": events}
    if db.get_setting(conn, "calendar_error"):
        out["status"] = f"alter Stand vom {dt.datetime.fromtimestamp(float(last), LOCAL):%d.%m. %H:%M}, Aktualisieren schlug fehl"
    return out
