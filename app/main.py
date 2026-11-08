"""Web-App. Alles ist gesperrt, außer dem, was ausdrücklich für den Login nötig ist."""
import asyncio
import datetime as dt
import io
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader

from . import auth, backup, coach, db, garmin, gcal, health, trainer, whoop
from .plan import SPORTS, match, monday

ACTIVITY_SPORTS = ("swim", "bike", "run", "strength", "other")

log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"
COOKIE = "__Host-tri_session"
ORIGIN = os.environ.get("TRI_ORIGIN", "https://training.example.org")
SYNC_INTERVAL_S = int(os.environ.get("TRI_SYNC_INTERVAL_S", 1800))
WHOOP_CALLBACK = "/api/whoop/callback"
# Der WHOOP-Rückweg kommt von außen, dabei schickt der Browser das strenge Sitzungs-Cookie nicht mit.
# Geschützt ist er stattdessen durch den einmaligen state, den nur eine angemeldete Sitzung erzeugen kann.
PUBLIC = {"/login", "/api/login", "/static/style.css", "/static/login.js", "/healthz", WHOOP_CALLBACK}
throttle = auth.Throttle()
sync_lock = threading.Lock()


def run_sync_guarded() -> dict | None:
    if not sync_lock.acquire(blocking=False):
        return None
    try:
        gcal.refresh(app.state.calendar_transport)
        whoop.run_sync(app.state.whoop_transport)
        return garmin.run_sync(app.state.garmin_transport)
    finally:
        sync_lock.release()


async def _sync_loop():
    while True:
        await asyncio.sleep(SYNC_INTERVAL_S)
        try:
            await asyncio.to_thread(run_sync_guarded)
        except Exception:
            log.exception("Automatischer Abgleich fehlgeschlagen")


@asynccontextmanager
async def lifespan(_):
    db.init()
    task = asyncio.create_task(_sync_loop()) if SYNC_INTERVAL_S > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.state.garmin_transport = None
app.state.calendar_transport = None
app.state.whoop_transport = None


def _client_ip(request: Request) -> str:
    # Uvicorn lauscht nur auf 127.0.0.1, der Header kommt daher immer von nginx.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD") and origin and origin != ORIGIN:
        return JSONResponse({"detail": "Ungültige Herkunft"}, status_code=403)
    if path not in PUBLIC:
        conn = db.connect()
        try:
            ok = auth.session_valid(conn, request.cookies.get(COOKIE))
        finally:
            conn.close()
        if not ok:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
    response: Response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


class Db:
    def __enter__(self):
        self.conn = db.connect()
        return self.conn

    def __exit__(self, *_):
        self.conn.close()


# --- Login ---------------------------------------------------------------

class LoginIn(BaseModel):
    password: str = Field(max_length=500)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = _client_ip(request)
    if throttle.blocked(ip):
        raise HTTPException(429, "Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen.")
    with Db() as conn:
        stored = db.get_setting(conn, "password_hash")
        if not auth.verify_password(body.password, stored):
            throttle.fail(ip)
            log.warning("Fehlgeschlagener Login von %s", ip)
            raise HTTPException(401, "Falsches Passwort.")
        token = auth.create_session(conn)
    throttle.clear(ip)
    response = JSONResponse({"ok": True})
    response.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400, secure=True,
                        httponly=True, samesite="strict", path="/")
    return response


@app.post("/api/logout")
def logout(request: Request):
    with Db() as conn:
        auth.delete_session(conn, request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


# --- Seite ---------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --- Plan ----------------------------------------------------------------

class PlanIn(BaseModel):
    date: dt.date
    sport: Literal[SPORTS]
    title: str = Field(default="", max_length=120)
    duration_min: int | None = Field(default=None, ge=0, le=1440)
    distance_km: float | None = Field(default=None, ge=0, le=1000)
    intensity: str = Field(default="", max_length=60)
    notes: str = Field(default="", max_length=2000)


def _plan_values(body: PlanIn):
    return (body.date.isoformat(), body.sport, body.title.strip(), body.duration_min,
            body.distance_km, body.intensity.strip(), body.notes.strip())


@app.post("/api/planned", status_code=201)
def create_plan(body: PlanIn):
    with Db() as conn:
        cur = conn.execute(
            "INSERT INTO planned(date,sport,title,duration_min,distance_km,intensity,notes) VALUES(?,?,?,?,?,?,?)",
            _plan_values(body))
        conn.commit()
        return {"id": cur.lastrowid}


def _refuse_google(conn, plan_id: int) -> None:
    row = conn.execute("SELECT google_uid FROM planned WHERE id=?", (plan_id,)).fetchone()
    if row and row["google_uid"]:
        raise HTTPException(403, "Diese Einheit kommt aus deinem Google Kalender. Ändere sie bitte dort.")


@app.put("/api/planned/{plan_id}")
def update_plan(plan_id: int, body: PlanIn):
    with Db() as conn:
        _refuse_google(conn, plan_id)
        cur = conn.execute(
            "UPDATE planned SET date=?,sport=?,title=?,duration_min=?,distance_km=?,intensity=?,notes=? WHERE id=?",
            (*_plan_values(body), plan_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Eintrag nicht gefunden")
    return {"ok": True}


class MoveIn(BaseModel):
    date: dt.date


@app.post("/api/planned/{plan_id}/move")
def move_plan(plan_id: int, body: MoveIn):
    with Db() as conn:
        _refuse_google(conn, plan_id)
        cur = conn.execute("UPDATE planned SET date=? WHERE id=?", (body.date.isoformat(), plan_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Eintrag nicht gefunden")
    return {"ok": True}


@app.delete("/api/planned/{plan_id}")
def delete_plan(plan_id: int):
    with Db() as conn:
        # Einheiten aus Google würden beim nächsten Abgleich zurückkommen, sie werden daher nur ausgeblendet.
        conn.execute("UPDATE planned SET hidden=1 WHERE id=? AND google_uid IS NOT NULL", (plan_id,))
        conn.execute("DELETE FROM planned WHERE id=? AND google_uid IS NULL", (plan_id,))
        conn.commit()
    return {"ok": True}


# --- Lesen ---------------------------------------------------------------

LIST_FIELDS = ("id,date,start_local,sport,type_key,name,duration_s,distance_m,avg_hr,max_hr,"
               "avg_power,norm_power,tss,te_aerobic,te_anaerobic,elevation_m,calories,avg_speed,avg_cadence")


def _range(conn, start: str, end: str):
    planned = [dict(r) for r in conn.execute(
        "SELECT * FROM planned WHERE hidden=0 AND date>=? AND date<=? ORDER BY date,id", (start, end))]
    activities = [dict(r) for r in conn.execute(
        f"SELECT {LIST_FIELDS} FROM activities WHERE date>=? AND date<=? ORDER BY start_local", (start, end))]
    return planned, activities


@app.get("/api/calendar")
def calendar(start: dt.date, end: dt.date):
    if (end - start).days > 62 or end < start:
        raise HTTPException(400, "Ungültiger Zeitraum")
    with Db() as conn:
        planned, activities = _range(conn, start.isoformat(), end.isoformat())
    matched = match(planned, activities, dt.date.today().isoformat())
    return {"planned": [{**p, **matched[p["id"]]} for p in planned], "activities": activities}


@app.get("/api/overview")
def overview():
    today = dt.date.today()
    week = monday(today)
    with Db() as conn:
        planned, activities = _range(conn, week.isoformat(), (week + dt.timedelta(days=6)).isoformat())
        name = db.get_setting(conn, "race_name", coach.DEFAULT_RACE_NAME)
        date = db.get_setting(conn, "race_date", coach.DEFAULT_RACE_DATE)
        last_sync = db.get_setting(conn, "last_sync_at")
        error = db.get_setting(conn, "last_sync_error", "")
        alerts = health.alerts(conn)
        calendar_only = coach.calendar_only_sessions(conn, week.isoformat(), (week + dt.timedelta(days=6)).isoformat(), today.isoformat())
        recovery = _recovery(conn, today)
    sports = {}
    for sport in ("swim", "bike", "run", "strength", "other"):
        sports[sport] = {"planned_min": 0, "done_min": 0}
    for plan in planned:
        minutes = plan["duration_min"] or 0
        if plan["sport"] == "brick":
            sports["bike"]["planned_min"] += minutes * 3 / 4
            sports["run"]["planned_min"] += minutes / 4
        elif plan["sport"] in sports:
            sports[plan["sport"]]["planned_min"] += minutes
    for activity in activities:
        sports[activity["sport"]]["done_min"] += (activity["duration_s"] or 0) / 60
    for session in calendar_only:
        for sport, seconds in coach.split_sport(session):
            if sport in sports:
                sports[sport]["done_min"] += seconds / 60
    return {
        "alerts": alerts,
        "today": today.isoformat(),
        "week_start": week.isoformat(),
        "race": {"name": name, "date": date, "days_left": (dt.date.fromisoformat(date) - today).days},
        "sports": {k: {a: round(b) for a, b in v.items()} for k, v in sports.items()},
        "recovery": recovery,
        "garmin": {"connected": garmin.read_key() is not None,
                   "last_sync_at": float(last_sync) if last_sync else None, "error": error},
    }


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=4000)


class ManualIn(NoteIn):
    date: dt.date
    time: str = Field(default="12:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    sport: Literal[ACTIVITY_SPORTS]
    name: str = Field(default="", max_length=120)
    duration_min: float = Field(gt=0, le=1440)
    distance_km: float | None = Field(default=None, gt=0, le=1000)
    avg_hr: int | None = Field(default=None, ge=30, le=250)
    max_hr: int | None = Field(default=None, ge=30, le=250)
    avg_power: int | None = Field(default=None, ge=0, le=2500)
    elevation_m: int | None = Field(default=None, ge=0, le=20000)
    calories: int | None = Field(default=None, ge=0, le=30000)


def _manual_values(body: ManualIn) -> tuple:
    if body.date > dt.date.today():
        raise HTTPException(400, "Ein absolviertes Training kann nicht in der Zukunft liegen. Dafür gibt es den Plan.")
    seconds = body.duration_min * 60
    meters = body.distance_km * 1000 if body.distance_km else None
    return (body.date.isoformat(), f"{body.date} {body.time}:00", body.sport, body.name.strip(), seconds, meters,
            meters / seconds if meters else None, body.avg_hr, body.max_hr, body.avg_power,
            body.elevation_m, body.calories, body.note.strip())


@app.post("/api/activities", status_code=201)
def create_manual(body: ManualIn):
    values = _manual_values(body)
    with Db() as conn:
        # Manuelle Einträge bekommen negative IDs und kollidieren so nie mit Garmin.
        new_id = conn.execute("SELECT MIN(MIN(id), 0) - 1 AS n FROM activities").fetchone()["n"] or -1
        conn.execute(
            "INSERT INTO activities(id,date,start_local,sport,name,duration_s,distance_m,avg_speed,avg_hr,max_hr,"
            "avg_power,elevation_m,calories,note,type_key,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'manual','{}')",
            (new_id, *values))
        conn.commit()
        garmin.queue_analysis(conn, "activity", str(new_id))
    return {"id": new_id}


@app.put("/api/activities/{activity_id}")
def update_manual(activity_id: int, body: ManualIn):
    if activity_id >= 0:
        raise HTTPException(403, "Garmin-Trainings lassen sich hier nicht ändern, nur die Notiz.")
    values = _manual_values(body)
    with Db() as conn:
        cur = conn.execute(
            "UPDATE activities SET date=?,start_local=?,sport=?,name=?,duration_s=?,distance_m=?,avg_speed=?,avg_hr=?,"
            "max_hr=?,avg_power=?,elevation_m=?,calories=?,note=? WHERE id=?", (*values, activity_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Aktivität nicht gefunden")
    return {"ok": True}


@app.delete("/api/activities/{activity_id}")
def delete_manual(activity_id: int):
    if activity_id >= 0:
        raise HTTPException(403, "Garmin-Trainings lassen sich hier nicht löschen.")
    with Db() as conn:
        conn.execute("DELETE FROM activities WHERE id=?", (activity_id,))
        conn.execute("DELETE FROM analyses WHERE scope='activity' AND ref=?", (str(activity_id),))
        conn.commit()
    return {"ok": True}


@app.put("/api/activities/{activity_id}/note")
def save_note(activity_id: int, body: NoteIn):
    with Db() as conn:
        cur = conn.execute("UPDATE activities SET note=? WHERE id=?", (body.note.strip(), activity_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Aktivität nicht gefunden")
    return {"ok": True}


@app.get("/api/activities")
def activities_list(limit: int = 15, offset: int = 0):
    limit = max(1, min(limit, 100))
    with Db() as conn:
        rows = conn.execute(
            f"""SELECT {LIST_FIELDS},
                (SELECT status FROM analyses x WHERE x.scope='activity' AND x.ref=CAST(a.id AS TEXT)
                 ORDER BY x.id DESC LIMIT 1) AS analysis_status
                FROM activities a ORDER BY start_local DESC LIMIT ? OFFSET ?""",
            (limit, max(offset, 0))).fetchall()
        result = [dict(r) for r in rows]
        if offset == 0:
            today = dt.date.today()
            start = (today - dt.timedelta(days=60)).isoformat()
            for plan in coach.calendar_only_sessions(conn, start, today.isoformat(), today.isoformat()):
                result.append({"calendar_only": True, "id": plan["id"], "date": plan["date"], "start_local": f"{plan['date']} 00:00:00",
                               "sport": plan["sport"], "name": plan["title"], "duration_s": (plan["duration_min"] or 0) * 60})
            result.sort(key=lambda row: row["start_local"], reverse=True)
            result = result[:limit]
    return result


def _latest_analysis(conn, scope: str, ref: str):
    row = conn.execute(
        "SELECT id,status,result,error,created_at,finished_at FROM analyses "
        "WHERE scope=? AND ref=? ORDER BY id DESC LIMIT 1", (scope, ref)).fetchone()
    return dict(row) if row else None


@app.get("/api/activities/{activity_id}")
def activity_detail(activity_id: int):
    with Db() as conn:
        row = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Aktivität nicht gefunden")
        activity = dict(row)
        planned, day = _range(conn, activity["date"], activity["date"])
        matched = match(planned, day, dt.date.today().isoformat())
        plan = [p for p in planned if activity_id in matched[p["id"]]["activity_ids"]]
        analysis = _latest_analysis(conn, "activity", str(activity_id))
    details = activity.pop("details")
    activity.pop("raw")
    return {**activity, "laps": garmin.laps(details), "plan": plan, "analysis": analysis}


@app.get("/api/trends")
def trends(weeks: int = 12):
    weeks = max(4, min(weeks, 52))
    today = dt.date.today()
    first = monday(today) - dt.timedelta(weeks=weeks - 1)
    with Db() as conn:
        rows = [dict(row) for row in conn.execute("SELECT date,sport,duration_s,distance_m,elevation_m FROM activities WHERE date>=?",
                                                  (first.isoformat(), )).fetchall()]
        for plan in coach.calendar_only_sessions(conn, first.isoformat(), today.isoformat(), today.isoformat()):
            for sport, seconds in coach.split_sport(plan):
                rows.append({"date": plan["date"], "sport": sport, "duration_s": seconds, "distance_m": None, "elevation_m": None})
        wellness = conn.execute(
            "SELECT date,sleep_s,sleep_score,hrv,resting_hr,whoop_recovery,whoop_hrv,whoop_resting_hr,whoop_sleep_s,whoop_strain "
            "FROM wellness WHERE date>=? ORDER BY date",
            ((today - dt.timedelta(days=42)).isoformat(),)).fetchall()
    series = [{"week": (first + dt.timedelta(weeks=i)).isoformat(), "swim": 0, "bike": 0, "run": 0,
               "strength": 0, "other": 0, "km": {}, "hm": {}, "count": {}} for i in range(weeks)]
    for row in rows:
        index = (dt.date.fromisoformat(row["date"]) - first).days // 7
        if 0 <= index < weeks:
            entry = series[index]
            sport = row["sport"]
            entry[sport] += (row["duration_s"] or 0) / 3600
            entry["km"][sport] = entry["km"].get(sport, 0) + (row["distance_m"] or 0) / 1000
            entry["hm"][sport] = entry["hm"].get(sport, 0) + (row["elevation_m"] or 0)
            entry["count"][sport] = entry["count"].get(sport, 0) + 1
    for entry in series:
        for sport in ("swim", "bike", "run", "strength", "other"):
            entry[sport] = round(entry[sport], 2)
        entry["km"] = {sport: round(value, 2) for sport, value in entry["km"].items()}
        entry["hm"] = {sport: round(value) for sport, value in entry["hm"].items()}
    return {"weeks": series, "wellness": [dict(r) for r in wellness]}


# --- Auswertung ----------------------------------------------------------

class AnalysisIn(BaseModel):
    scope: Literal["activity", "week"]
    ref: str = Field(max_length=20)


@app.post("/api/analyses", status_code=202)
def request_analysis(body: AnalysisIn):
    with Db() as conn:
        if body.scope == "activity":
            digits = body.ref.removeprefix("-")
            if not digits.isdecimal() or not conn.execute(
                    "SELECT 1 FROM activities WHERE id=?", (int(body.ref),)).fetchone():
                raise HTTPException(404, "Aktivität nicht gefunden")
        else:
            try:
                dt.date.fromisoformat(body.ref)
            except ValueError:
                raise HTTPException(400, "Ungültige Woche") from None
        return {"id": garmin.queue_analysis(conn, body.scope, body.ref)}


@app.get("/api/analyses/latest")
def latest_analysis(scope: Literal["activity", "week"], ref: str):
    with Db() as conn:
        return _latest_analysis(conn, scope, ref)


# --- Beschwerden ------------------------------------------------------------

class ComplaintIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    started_on: dt.date
    cause: str = Field(default="", max_length=2000)


class ComplaintNew(ComplaintIn):
    text: str = Field(default="", max_length=2000)
    severity: int | None = Field(default=None, ge=0, le=10)


class EntryIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    severity: int | None = Field(default=None, ge=0, le=10)


def _check_started(day: dt.date) -> None:
    if day > dt.date.today():
        raise HTTPException(400, "Eine Beschwerde kann nicht in der Zukunft beginnen.")


def _complaint_exists(conn, complaint_id: int) -> None:
    if not conn.execute("SELECT 1 FROM complaints WHERE id=?", (complaint_id,)).fetchone():
        raise HTTPException(404, "Beschwerde nicht gefunden")


@app.get("/api/complaints")
def complaints_list():
    with Db() as conn:
        rows = conn.execute("SELECT * FROM complaints ORDER BY resolved_on IS NOT NULL, started_on DESC, id DESC").fetchall()
        out = []
        for row in rows:
            entries = conn.execute("SELECT id,date,severity,text FROM complaint_entries WHERE complaint_id=? ORDER BY id DESC",
                                   (row["id"],)).fetchall()
            out.append({**dict(row), "entries": [dict(e) for e in entries]})
    return out


@app.post("/api/complaints", status_code=201)
def complaint_create(body: ComplaintNew):
    _check_started(body.started_on)
    with Db() as conn:
        cur = conn.execute("INSERT INTO complaints(name,started_on,cause) VALUES(?,?,?)",
                           (body.name.strip(), body.started_on.isoformat(), body.cause.strip()))
        if body.text.strip():
            conn.execute("INSERT INTO complaint_entries(complaint_id,date,severity,text) VALUES(?,?,?,?)",
                         (cur.lastrowid, dt.date.today().isoformat(), body.severity, body.text.strip()))
        conn.commit()
    return {"id": cur.lastrowid}


@app.put("/api/complaints/{complaint_id}")
def complaint_update(complaint_id: int, body: ComplaintIn):
    _check_started(body.started_on)
    with Db() as conn:
        _complaint_exists(conn, complaint_id)
        conn.execute("UPDATE complaints SET name=?,started_on=?,cause=? WHERE id=?",
                     (body.name.strip(), body.started_on.isoformat(), body.cause.strip(), complaint_id))
        conn.commit()
    return {"ok": True}


@app.post("/api/complaints/{complaint_id}/entries", status_code=201)
def complaint_entry(complaint_id: int, body: EntryIn):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Der Eintrag ist leer.")
    with Db() as conn:
        _complaint_exists(conn, complaint_id)
        conn.execute("INSERT INTO complaint_entries(complaint_id,date,severity,text) VALUES(?,?,?,?)",
                     (complaint_id, dt.date.today().isoformat(), body.severity, text))
        conn.commit()
    return {"ok": True}


@app.delete("/api/complaints/{complaint_id}/entries/{entry_id}")
def complaint_entry_delete(complaint_id: int, entry_id: int):
    with Db() as conn:
        conn.execute("DELETE FROM complaint_entries WHERE id=? AND complaint_id=?", (entry_id, complaint_id))
        conn.commit()
    return {"ok": True}


@app.post("/api/complaints/{complaint_id}/resolve")
def complaint_resolve(complaint_id: int):
    with Db() as conn:
        _complaint_exists(conn, complaint_id)
        conn.execute("UPDATE complaints SET resolved_on=? WHERE id=?", (dt.date.today().isoformat(), complaint_id))
        conn.commit()
    return {"ok": True}


@app.post("/api/complaints/{complaint_id}/reopen")
def complaint_reopen(complaint_id: int):
    with Db() as conn:
        _complaint_exists(conn, complaint_id)
        conn.execute("UPDATE complaints SET resolved_on=NULL WHERE id=?", (complaint_id,))
        conn.commit()
    return {"ok": True}


@app.delete("/api/complaints/{complaint_id}")
def complaint_delete(complaint_id: int):
    with Db() as conn:
        conn.execute("DELETE FROM complaint_entries WHERE complaint_id=?", (complaint_id,))
        conn.execute("DELETE FROM complaints WHERE id=?", (complaint_id,))
        conn.commit()
    return {"ok": True}


# --- Heute ---------------------------------------------------------------

RECOVERY_FIELDS = {"garmin": {"sleep_s": "sleep_s", "hrv": "hrv", "resting_hr": "resting_hr"},
                   "whoop": {"recovery": "whoop_recovery", "sleep_s": "whoop_sleep_s", "hrv": "whoop_hrv",
                             "resting_hr": "whoop_resting_hr", "strain": "whoop_strain"}}


def _recovery(conn, today: dt.date) -> dict:
    """Letzter Wert je Messgröße aus den letzten 3 Tagen und der Schnitt der 28 Tage davor.
    Mit WHOOP-Daten zeigt die Kachel WHOOP, sonst Garmin. Beide HRV-Werte sind nicht vergleichbar."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM wellness WHERE date>=? AND date<=? ORDER BY date DESC",
        ((today - dt.timedelta(days=31)).isoformat(), today.isoformat()))]
    recent_from = (today - dt.timedelta(days=2)).isoformat()
    source = "whoop" if any(r["whoop_recovery"] is not None for r in rows) else "garmin"
    out = {"source": source}
    for name, field in RECOVERY_FIELDS[source].items():
        values = [r for r in rows if r[field] is not None]
        latest = values[0] if values and values[0]["date"] >= recent_from else None
        before = [r[field] for r in values if latest is None or r["date"] < latest["date"]]
        out[name] = {
            "value": latest[field] if latest else None,
            "date": latest["date"] if latest else None,
            "average": round(sum(before) / len(before), 1) if len(before) >= 5 else None,
        }
    return out


@app.get("/api/today")
def today_view():
    today = dt.date.today().isoformat()
    with Db() as conn:
        planned, activities = _range(conn, today, today)
        status = match(planned, activities, today)
        following = conn.execute(
            "SELECT * FROM planned WHERE hidden=0 AND date>? AND sport<>'rest' ORDER BY date,id LIMIT 1", (today,)).fetchone()
        recovery = _recovery(conn, dt.date.today())
    return {"date": today, "planned": [{**p, **status[p["id"]]} for p in planned], "activities": activities,
            "next": dict(following) if following else None, "recovery": recovery}


# --- Trainer -------------------------------------------------------------

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ApplyIn(BaseModel):
    replace: bool = False


def _chat_view(conn, row) -> dict:
    message = dict(row)
    proposal = json.loads(message["proposal"]) if message["proposal"] else None
    message["proposal"] = proposal
    if proposal and message["proposal_status"] == "open":
        message["conflicts"] = trainer.conflicts(conn, proposal)
    return message


@app.get("/api/chat")
def chat_list():
    with Db() as conn:
        rows = conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT 100").fetchall()
        return [_chat_view(conn, row) for row in reversed(rows)]


@app.post("/api/chat", status_code=202)
def chat_send(body: ChatIn):
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Die Nachricht ist leer.")
    # Der Trainer soll mit möglichst frischen Terminen antworten. Ein Fehler hier stoppt den Chat nicht.
    gcal.refresh_if_stale(app.state.calendar_transport)
    with Db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM chat_messages WHERE status IN ('pending','running')").fetchone():
            conn.rollback()
            raise HTTPException(409, "Dein Trainer antwortet gerade noch.")
        now = time.time()
        conn.execute("INSERT INTO chat_messages(role,content,status,created_at) VALUES('user',?,'done',?)", (text, now))
        conn.execute("INSERT INTO chat_messages(role,status,created_at) VALUES('assistant','pending',?)", (now,))
        conn.commit()
    return {"ok": True}


@app.post("/api/chat/{message_id}/retry", status_code=202)
def chat_retry(message_id: int):
    with Db() as conn:
        cur = conn.execute("UPDATE chat_messages SET status='pending',error=NULL WHERE id=? AND status='failed'", (message_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Nachricht nicht gefunden")
    return {"ok": True}


def _open_proposal(conn, message_id: int) -> list[dict]:
    row = conn.execute("SELECT proposal,proposal_status FROM chat_messages WHERE id=?", (message_id,)).fetchone()
    if not row or not row["proposal"]:
        raise HTTPException(404, "Vorschlag nicht gefunden")
    if row["proposal_status"] != "open":
        raise HTTPException(409, "Dieser Vorschlag wurde schon bearbeitet.")
    return json.loads(row["proposal"])


@app.post("/api/chat/{message_id}/apply")
def chat_apply(message_id: int, body: ApplyIn):
    with Db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            proposal = _open_proposal(conn, message_id)
            result = trainer.apply_proposal(conn, proposal, body.replace)
        except Exception:
            conn.rollback()
            raise
        conn.execute("UPDATE chat_messages SET proposal_status='applied' WHERE id=?", (message_id,))
        conn.commit()
    return {**result, "first_date": proposal[0]["date"]}


@app.post("/api/chat/{message_id}/discard")
def chat_discard(message_id: int):
    with Db() as conn:
        _open_proposal(conn, message_id)
        conn.execute("UPDATE chat_messages SET proposal_status='discarded' WHERE id=?", (message_id,))
        conn.commit()
    return {"ok": True}


@app.delete("/api/chat")
def chat_clear():
    with Db() as conn:
        if conn.execute("SELECT 1 FROM chat_messages WHERE status IN ('pending','running')").fetchone():
            raise HTTPException(409, "Dein Trainer antwortet gerade noch.")
        conn.execute("DELETE FROM chat_messages")
        conn.commit()
    return {"ok": True}


# --- Wissen des Trainers --------------------------------------------------

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_CHARS = 100000
TEXT_TYPES = (".txt", ".md", ".markdown", ".csv", ".json", ".tsv", ".log", ".html")


class InstructionsIn(BaseModel):
    instructions: str = Field(default="", max_length=trainer.MAX_INSTRUCTION_CHARS)


class KnowledgeIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)


def _extract_text(name: str, data: bytes) -> str:
    lower = name.lower()
    if lower.endswith(".pdf"):
        try:
            pages = PdfReader(io.BytesIO(data)).pages
            text = "\n\n".join((page.extract_text() or "") for page in pages)
        except Exception:
            raise HTTPException(400, f"{name} ließ sich nicht als PDF lesen.") from None
        if not text.strip():
            raise HTTPException(400, f"{name} enthält keinen lesbaren Text, vermutlich ist es ein Scan.")
        return text
    if lower.endswith(TEXT_TYPES):
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return data.decode("latin-1")
    raise HTTPException(400, "Unterstützt werden PDF und Textdateien wie TXT, MD oder CSV. Word- und Excel-Dateien bitte als PDF oder CSV speichern.")


def _save_document(title: str, content: str) -> dict:
    text = content.strip()
    if not text:
        raise HTTPException(400, "Das Dokument ist leer.")
    with Db() as conn:
        cur = conn.execute("INSERT INTO trainer_knowledge(title,content,created_at) VALUES(?,?,?)",
                           (title.strip()[:120], text[:MAX_DOCUMENT_CHARS], time.time()))
        conn.commit()
    return {"id": cur.lastrowid, "truncated": len(text) > MAX_DOCUMENT_CHARS}


@app.get("/api/knowledge")
def knowledge_list():
    with Db() as conn:
        rows = conn.execute("SELECT id,title,LENGTH(content) AS chars FROM trainer_knowledge ORDER BY id").fetchall()
        return {"instructions": db.get_setting(conn, "trainer_instructions", ""),
                "documents": [dict(r) for r in rows], "limit": trainer.MAX_KNOWLEDGE_CHARS}


@app.put("/api/knowledge/instructions")
def knowledge_instructions(body: InstructionsIn):
    with Db() as conn:
        db.set_setting(conn, "trainer_instructions", body.instructions.strip())
        conn.commit()
    return {"ok": True}


@app.post("/api/knowledge", status_code=201)
def knowledge_add(body: KnowledgeIn):
    return _save_document(body.title, body.content)


@app.post("/api/knowledge/file", status_code=201)
async def knowledge_upload(request: Request, name: str):
    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Die Datei ist größer als 10 MB.")
    if not data:
        raise HTTPException(400, "Die Datei ist leer.")
    title = Path(name).name
    text = await asyncio.to_thread(_extract_text, title, data)
    return _save_document(title, text)


@app.delete("/api/knowledge/{document_id}")
def knowledge_delete(document_id: int):
    with Db() as conn:
        conn.execute("DELETE FROM trainer_knowledge WHERE id=?", (document_id,))
        conn.commit()
    return {"ok": True}


# --- Einstellungen und Abgleich ------------------------------------------

class SettingsIn(BaseModel):
    race_name: str = Field(min_length=1, max_length=80)
    race_date: dt.date
    athlete_notes: str = Field(default="", max_length=4000)


class KeyIn(BaseModel):
    key: str = Field(min_length=8, max_length=2000)
    valid_days: int | None = Field(default=None, ge=1, le=366)


class CalendarIn(BaseModel):
    urls: str = Field(min_length=1, max_length=4000)


class CalendarTermsIn(BaseModel):
    training: str = Field(default="", max_length=2000)
    ignore: str = Field(default="", max_length=2000)


class KeyExpiryIn(BaseModel):
    expires_on: dt.date | None = None


@app.get("/api/settings")
def get_settings():
    with Db() as conn:
        return {
            "race_name": db.get_setting(conn, "race_name", coach.DEFAULT_RACE_NAME),
            "race_date": db.get_setting(conn, "race_date", coach.DEFAULT_RACE_DATE),
            "athlete_notes": db.get_setting(conn, "athlete_notes", ""),
            "garmin_key_set": garmin.read_key() is not None,
            "garmin_key_expires": db.get_setting(conn, "garmin_key_expires", ""),
            "calendar_count": len(gcal.read_urls()),
            "calendar_training_terms": db.get_setting(conn, "calendar_training_terms", ""),
            "calendar_ignore_terms": db.get_setting(conn, "calendar_ignore_terms", ""),
            "calendar_hidden": conn.execute("SELECT COUNT(*) c FROM planned WHERE hidden=1").fetchone()["c"],
            "calendar_last_fetch": float(db.get_setting(conn, "calendar_last_fetch") or 0) or None,
            "calendar_error": db.get_setting(conn, "calendar_error", ""),
            "whoop_connected": whoop.connected(),
            "whoop_client_id": whoop.read_config().get("client_id", ""),
            "whoop_redirect_uri": ORIGIN + WHOOP_CALLBACK,
            "whoop_last_sync_at": float(db.get_setting(conn, "whoop_last_sync_at") or 0) or None,
            "whoop_error": db.get_setting(conn, "whoop_error", ""),
            "last_backup_at": float(db.get_setting(conn, "last_backup_at") or 0) or None,
            "backup_count": len(backup.list_backups()),
        }


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    with Db() as conn:
        db.set_setting(conn, "race_name", body.race_name.strip())
        db.set_setting(conn, "race_date", body.race_date.isoformat())
        db.set_setting(conn, "athlete_notes", body.athlete_notes.strip())
        conn.commit()
    return {"ok": True}


@app.post("/api/settings/garmin-key")
def put_key(body: KeyIn):
    key = body.key.strip()
    client = garmin.Client(key, app.state.garmin_transport)
    try:
        client.verify()
    except garmin.KeyRejected as exc:
        raise HTTPException(400, str(exc)) from None
    except garmin.GarminError as exc:
        raise HTTPException(502, f"Key konnte nicht geprüft werden: {exc}") from None
    finally:
        client.close()
    garmin.save_key(key)
    expiry = (dt.date.today() + dt.timedelta(days=body.valid_days)).isoformat() if body.valid_days else ""
    with Db() as conn:
        db.set_setting(conn, "garmin_key_expires", expiry)
        db.set_setting(conn, "garmin_key_expiry_hint", "")
        db.set_setting(conn, "last_sync_error", "")
        conn.commit()
    threading.Thread(target=run_sync_guarded, daemon=True).start()
    return {"ok": True}


@app.post("/api/settings/calendar")
def put_calendar(body: CalendarIn):
    urls = list(dict.fromkeys(gcal.normalize_url(line) for line in body.urls.splitlines() if line.strip()))
    if not urls or len(urls) > gcal.MAX_URLS:
        raise HTTPException(400, f"Bitte 1 bis {gcal.MAX_URLS} Adressen eintragen, eine pro Zeile.")
    for url in urls:
        if not gcal.valid_url(url):
            raise HTTPException(400, gcal.explain_bad_url(url))
    try:
        events = gcal.load_all(urls, dt.date.today(), app.state.calendar_transport)
    except gcal.CalendarError as exc:
        raise HTTPException(400, str(exc)) from None
    gcal.save_urls(urls)
    with Db() as conn:
        counts = gcal.store(conn, events)
        conn.commit()
    return {"calendars": len(urls), **counts}


@app.put("/api/settings/calendar-terms")
def put_calendar_terms(body: CalendarTermsIn):
    with Db() as conn:
        db.set_setting(conn, "calendar_training_terms", body.training.strip())
        db.set_setting(conn, "calendar_ignore_terms", body.ignore.strip())
        conn.commit()
    gcal.refresh(app.state.calendar_transport)
    return {"ok": True}


@app.post("/api/settings/calendar-unhide")
def calendar_unhide():
    with Db() as conn:
        conn.execute("UPDATE planned SET hidden=0 WHERE hidden=1")
        conn.commit()
    return {"ok": True}


@app.delete("/api/settings/calendar")
def remove_calendar():
    gcal.disconnect()
    return {"ok": True}


@app.put("/api/settings/garmin-key-expiry")
def put_key_expiry(body: KeyExpiryIn):
    with Db() as conn:
        db.set_setting(conn, "garmin_key_expires", body.expires_on.isoformat() if body.expires_on else "")
        conn.commit()
    return {"ok": True}


@app.delete("/api/settings/garmin-key")
def remove_key():
    garmin.delete_key()
    with Db() as conn:
        db.set_setting(conn, "garmin_key_expires", "")
        db.set_setting(conn, "garmin_key_expiry_hint", "")
        db.set_setting(conn, "last_sync_error", "")
        conn.commit()
    return {"ok": True}


class WhoopIn(BaseModel):
    client_id: str = Field(min_length=8, max_length=200, pattern=r"^\S+$")
    client_secret: str = Field(default="", max_length=500, pattern=r"^\S*$")


@app.post("/api/settings/whoop")
def start_whoop(body: WhoopIn):
    """Gibt die WHOOP-Anmeldeseite zurück. Ohne neues Secret gilt das gespeicherte."""
    secret = body.client_secret or whoop.read_config().get("client_secret", "")
    if not secret:
        raise HTTPException(400, "Bitte das Client Secret eintragen.")
    with Db() as conn:
        return {"url": whoop.start_auth(conn, body.client_id, secret, ORIGIN + WHOOP_CALLBACK)}


def _whoop_page(text: str, ok: bool) -> Response:
    # Erst eine eigene Seite, dann weiter: So schickt der Browser das Sitzungs-Cookie wieder mit.
    html = ("<!DOCTYPE html><html lang=\"de\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta http-equiv=\"refresh\" content=\"%s;url=/\"><title>WHOOP</title>"
            "<link rel=\"stylesheet\" href=\"/static/style.css\"></head><body class=\"login-page\"><main class=\"card login-card\"><h1>Trainings-Dashboard</h1>"
            "<p>%s</p><p><a href=\"/\">Zum Dashboard</a></p></main></body></html>") % (1 if ok else 30, text)
    return Response(html, media_type="text/html", status_code=200 if ok else 400)


@app.get(WHOOP_CALLBACK)
def whoop_callback(state: str = "", code: str = "", error: str = ""):
    with Db() as conn:
        valid = whoop.take_state(conn, state)
    if not valid:
        return _whoop_page("Diese Anmeldung ist abgelaufen oder ungültig. Starte sie in den Einstellungen neu.", False)
    if error or not code:
        return _whoop_page("WHOOP hat den Zugriff nicht freigegeben.", False)
    try:
        whoop.finish_auth(code, ORIGIN + WHOOP_CALLBACK, app.state.whoop_transport)
    except whoop.WhoopError as exc:
        log.warning("WHOOP-Anmeldung: %s", exc)
        return _whoop_page("Die Anmeldung bei WHOOP hat nicht geklappt. Prüfe Client ID und Secret und versuche es erneut.", False)
    with Db() as conn:
        db.set_setting(conn, "whoop_error", "")
        db.set_setting(conn, "whoop_initial_sync_done", "")
        conn.commit()
    threading.Thread(target=run_sync_guarded, daemon=True).start()
    return _whoop_page("WHOOP ist verbunden. Die Erholungsdaten der letzten 60 Tage werden geladen.", True)


@app.delete("/api/settings/whoop")
def remove_whoop():
    whoop.disconnect(app.state.whoop_transport)
    with Db() as conn:
        db.set_setting(conn, "whoop_error", "")
        db.set_setting(conn, "whoop_last_sync_at", "")
        db.set_setting(conn, "whoop_initial_sync_done", "")
        conn.commit()
    return {"ok": True}


@app.post("/api/sync")
def sync_now():
    result = run_sync_guarded()
    if result is None:
        raise HTTPException(409, "Ein Abgleich läuft bereits.")
    return result
