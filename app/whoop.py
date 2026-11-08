"""WHOOP Developer API (v2): OAuth-Verbindung, Erholung, Schlaf und Tagesbelastung holen.

Die Zugangsdaten liegen in `data/whoop.json` (nur für `triathlon` lesbar) und werden nie angezeigt.
WHOOP tauscht bei jeder Erneuerung auch den Refresh-Token aus, darum wird er sofort gespeichert
und nie parallel erneuert.
"""
import datetime as dt
import json
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx

from . import db
from .garmin import _upsert_wellness

log = logging.getLogger("whoop")
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_URL = "https://api.prod.whoop.com/developer"
SCOPES = "offline read:recovery read:cycles read:sleep"
STATE_MAX_AGE_S = 15 * 60
FIRST_SYNC_DAYS = 60
REGULAR_SYNC_DAYS = 7
MAX_PAGES = 40
_token_lock = threading.Lock()


class WhoopError(Exception):
    pass


class AuthRejected(WhoopError):
    pass


def config_path():
    return db.DATA_DIR / "whoop.json"


def read_config() -> dict:
    try:
        return json.loads(config_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_config(config: dict) -> None:
    path = config_path()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    os.replace(tmp, path)


def delete_config() -> None:
    config_path().unlink(missing_ok=True)


def connected() -> bool:
    return bool(read_config().get("refresh_token"))


# --- OAuth ---------------------------------------------------------------

def start_auth(conn, client_id: str, client_secret: str, redirect_uri: str) -> str:
    """Merkt sich App-Daten und einen einmaligen state, gibt die Adresse für den WHOOP-Login zurück."""
    config = read_config()
    config.update(client_id=client_id, client_secret=client_secret)
    save_config(config)
    # WHOOP verlangt einen state mit genau 8 Zeichen. Er gilt nur kurz und nur einmal.
    state = secrets.token_urlsafe(6)
    db.set_setting(conn, "whoop_oauth_state", f"{state}|{time.time()}")
    conn.commit()
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": client_id,
                                       "redirect_uri": redirect_uri, "scope": SCOPES, "state": state})


def take_state(conn, state: str | None) -> bool:
    """Nur der passende state wird verbraucht, fremde Aufrufe können eine laufende Anmeldung nicht stören."""
    stored = db.get_setting(conn, "whoop_oauth_state", "")
    if not stored or not state:
        return False
    expected, created = stored.split("|", 1)
    if not secrets.compare_digest(expected.encode(), state.encode()):
        return False
    db.set_setting(conn, "whoop_oauth_state", "")
    conn.commit()
    return time.time() - float(created) < STATE_MAX_AGE_S


def _token_request(data: dict, transport=None) -> dict:
    try:
        with httpx.Client(timeout=30, transport=transport) as http:
            r = http.post(TOKEN_URL, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise WhoopError(f"WHOOP nicht erreichbar ({type(exc).__name__})") from exc
    if r.status_code in (400, 401, 403):
        raise AuthRejected("WHOOP lehnt die Anmeldung ab. Verbinde WHOOP in den Einstellungen neu.")
    if r.status_code >= 400:
        raise WhoopError(f"Token-Abruf fehlgeschlagen (HTTP {r.status_code})")
    body = r.json()
    if not body.get("access_token") or not body.get("refresh_token"):
        raise WhoopError("WHOOP hat keinen Refresh-Token geliefert.")
    return body


def _store_tokens(config: dict, body: dict) -> dict:
    config.update(access_token=body["access_token"], refresh_token=body["refresh_token"],
                  expires_at=time.time() + float(body.get("expires_in") or 3600))
    save_config(config)
    return config


def finish_auth(code: str, redirect_uri: str, transport=None) -> None:
    config = read_config()
    if not config.get("client_id"):
        raise WhoopError("Client ID fehlt. Starte die Verbindung in den Einstellungen neu.")
    body = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                           "client_id": config["client_id"], "client_secret": config["client_secret"]}, transport)
    _store_tokens(config, body)


def access_token(transport=None, force=False) -> str:
    with _token_lock:
        config = read_config()
        if not config.get("refresh_token"):
            raise AuthRejected("WHOOP ist nicht verbunden.")
        if force or not config.get("access_token") or config.get("expires_at", 0) - time.time() < 300:
            body = _token_request({"grant_type": "refresh_token", "refresh_token": config["refresh_token"],
                                   "client_id": config["client_id"], "client_secret": config["client_secret"],
                                   "scope": "offline"}, transport)
            config = _store_tokens(config, body)
        return config["access_token"]


def disconnect(transport=None) -> None:
    """Entzieht der App bei WHOOP den Zugriff (wenn möglich) und löscht alles Lokale."""
    try:
        token = access_token(transport)
        with httpx.Client(base_url=API_URL, timeout=15, transport=transport) as http:
            http.delete("/v2/user/access", headers={"Authorization": f"Bearer {token}"})
    except (WhoopError, httpx.HTTPError) as exc:
        log.warning("Zugriff bei WHOOP nicht entzogen: %s", exc)
    delete_config()


# --- Daten ---------------------------------------------------------------

class Client:
    def __init__(self, transport=None):
        self.transport = transport
        self.http = httpx.Client(base_url=API_URL, timeout=30, transport=transport)

    def close(self):
        self.http.close()

    def get(self, path: str, params: dict | None = None):
        token = access_token(self.transport)
        for attempt in (1, 2):
            try:
                r = self.http.get(path, params=params, headers={"Authorization": f"Bearer {token}",
                                                                "Accept": "application/json"})
            except httpx.HTTPError as exc:
                raise WhoopError(f"WHOOP nicht erreichbar ({type(exc).__name__})") from exc
            if r.status_code == 401 and attempt == 1:
                token = access_token(self.transport, force=True)
                continue
            break
        if r.status_code == 401:
            raise AuthRejected("WHOOP lehnt den Zugriff ab. Verbinde WHOOP in den Einstellungen neu.")
        if r.status_code == 429:
            raise WhoopError("WHOOP-Ratelimit erreicht (HTTP 429), nächster Versuch beim nächsten Abgleich.")
        if r.status_code >= 400:
            raise WhoopError(f"{path}: HTTP {r.status_code}")
        return r.json() if r.content else None

    def collection(self, path: str, start: dt.datetime) -> list[dict]:
        out, token = [], None
        for _ in range(MAX_PAGES):
            params = {"limit": 25, "start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            if token:
                params["nextToken"] = token
            page = self.get(path, params) or {}
            out.extend(r for r in page.get("records") or [] if isinstance(r, dict))
            token = page.get("next_token") or page.get("nextToken")
            if not token:
                break
        return out


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def local_date(stamp: str | None, offset: str | None) -> str | None:
    """Kalendertag am Ort der Messung, z. B. für das Aufwachen."""
    if not stamp:
        return None
    try:
        moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        zone = dt.datetime.fromisoformat(f"2000-01-01T00:00:00{offset}").tzinfo if offset else None
    except ValueError:
        return None
    return moment.astimezone(zone or dt.timezone.utc).date().isoformat()


def _sleep_seconds(score: dict) -> float | None:
    stages = score.get("stage_summary") or {}
    parts = [_num(stages.get(k)) for k in ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
                                            "total_rem_sleep_time_milli")]
    if all(p is None for p in parts):
        return None
    return sum(p or 0 for p in parts) / 1000


def sync(conn, client: Client, today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    first = db.get_setting(conn, "whoop_initial_sync_done") != "1"
    start_day = today - dt.timedelta(days=FIRST_SYNC_DAYS if first else REGULAR_SYNC_DAYS)
    start = dt.datetime.combine(start_day - dt.timedelta(days=1), dt.time(), dt.timezone.utc)

    # Schlaf zählt für den Tag des Aufwachens, wie bei Garmin. Nickerchen bleiben außen vor.
    wake_day, best = {}, {}
    for sleep in client.collection("/v2/activity/sleep", start):
        day = local_date(sleep.get("end"), sleep.get("timezone_offset"))
        if not day or sleep.get("nap"):
            continue
        wake_day[sleep.get("id")] = day
        score = sleep.get("score") if sleep.get("score_state") == "SCORED" else None
        if not score:
            continue
        seconds = _sleep_seconds(score)
        if day in best and (best[day][0] or 0) >= (seconds or 0):
            continue
        best[day] = (seconds, score)
    for day, (seconds, score) in best.items():
        _upsert_wellness(conn, day, whoop_sleep_s=seconds,
                         whoop_sleep_performance=_num(score.get("sleep_performance_percentage")),
                         whoop_resp_rate=_num(score.get("respiratory_rate")))

    cycle_day = {}
    for cycle in client.collection("/v2/cycle", start):
        day = local_date(cycle.get("start"), cycle.get("timezone_offset"))
        cycle_day[cycle.get("id")] = day
        score = cycle.get("score") if cycle.get("score_state") == "SCORED" else None
        if day and score:
            _upsert_wellness(conn, day, whoop_strain=_num(score.get("strain")))

    for recovery in client.collection("/v2/recovery", start):
        day = wake_day.get(recovery.get("sleep_id")) or cycle_day.get(recovery.get("cycle_id"))
        score = recovery.get("score") if recovery.get("score_state") == "SCORED" else None
        if not day or not score or score.get("user_calibrating"):
            continue
        _upsert_wellness(conn, day, whoop_recovery=_num(score.get("recovery_score")),
                         whoop_hrv=_num(score.get("hrv_rmssd_milli")),
                         whoop_resting_hr=_num(score.get("resting_heart_rate")),
                         whoop_spo2=_num(score.get("spo2_percentage")),
                         whoop_skin_temp=_num(score.get("skin_temp_celsius")))
    db.set_setting(conn, "whoop_initial_sync_done", "1")
    conn.commit()


def run_sync(transport=None) -> None:
    if not connected():
        return
    conn = db.connect()
    client = Client(transport)
    try:
        sync(conn, client)
        db.set_setting(conn, "whoop_last_sync_at", str(time.time()))
        db.set_setting(conn, "whoop_error", "")
    except WhoopError as exc:
        db.set_setting(conn, "whoop_error", str(exc))
    finally:
        client.close()
        conn.commit()
        conn.close()
