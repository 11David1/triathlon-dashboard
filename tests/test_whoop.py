import datetime as dt
import stat
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import db, health, whoop
from app.main import app


class FakeWhoop:
    """Spielt WHOOPs OAuth und die v2-API nach, mit rotierenden Refresh-Tokens."""

    def __init__(self):
        self.refresh = "r0"
        self.access = "a0"
        self.counter = 0
        self.calls = []
        self.revoked = False
        self.sleeps = [
            {"id": "s1", "cycle_id": 11, "start": "2026-11-05T22:30:00.000Z", "end": "2026-11-06T05:45:00.000Z",
             "timezone_offset": "+01:00", "nap": False, "score_state": "SCORED",
             "score": {"stage_summary": {"total_light_sleep_time_milli": 3 * 3600_000, "total_slow_wave_sleep_time_milli": 3600_000,
                                         "total_rem_sleep_time_milli": 2 * 3600_000},
                       "sleep_performance_percentage": 88, "respiratory_rate": 15.1}},
            {"id": "nap", "cycle_id": 11, "start": "2026-11-06T13:00:00.000Z", "end": "2026-11-06T13:30:00.000Z",
             "timezone_offset": "+01:00", "nap": True, "score_state": "SCORED", "score": {"stage_summary": {}}},
            # Aufwachen kurz nach Mitternacht UTC, am Ort aber schon der nächste Tag
            {"id": "s2", "cycle_id": 12, "start": "2026-11-06T17:00:00.000Z", "end": "2026-11-06T23:30:00.000Z",
             "timezone_offset": "+08:00", "nap": False, "score_state": "PENDING_SCORE", "score": None},
        ]
        self.recoveries = [
            {"cycle_id": 11, "sleep_id": "s1", "score_state": "SCORED",
             "score": {"user_calibrating": False, "recovery_score": 72, "resting_heart_rate": 46,
                       "hrv_rmssd_milli": 81.5, "spo2_percentage": 96.2, "skin_temp_celsius": 33.4}},
            {"cycle_id": 12, "sleep_id": "s2", "score_state": "SCORED",
             "score": {"user_calibrating": True, "recovery_score": 10}},
        ]
        self.cycles = [{"id": 11, "start": "2026-11-06T05:45:00.000Z", "timezone_offset": "+01:00",
                        "score_state": "SCORED", "score": {"strain": 12.4}}]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.path == "/oauth/oauth2/token":
            form = parse_qs(request.content.decode())
            if form["client_secret"] != ["secret-xyz"]:
                return httpx.Response(401)
            if form["grant_type"] == ["authorization_code"]:
                if form["code"] != ["good-code"] or form["redirect_uri"] != ["https://training.example.org/api/whoop/callback"]:
                    return httpx.Response(400)
            elif form["refresh_token"] != [self.refresh]:
                return httpx.Response(400)
            self.counter += 1
            self.refresh, self.access = f"r{self.counter}", f"a{self.counter}"
            return httpx.Response(200, json={"access_token": self.access, "refresh_token": self.refresh,
                                             "expires_in": 3600, "token_type": "bearer"})
        if request.headers.get("authorization") != f"Bearer {self.access}":
            return httpx.Response(401)
        path = request.url.path.removeprefix("/developer")
        if path == "/v2/user/access" and request.method == "DELETE":
            self.revoked = True
            return httpx.Response(204)
        data = {"/v2/activity/sleep": self.sleeps, "/v2/recovery": self.recoveries, "/v2/cycle": self.cycles}.get(path)
        if data is None:
            return httpx.Response(404)
        offset = int(request.url.params.get("nextToken", 0))
        page = data[offset:offset + 2]
        nxt = str(offset + 2) if offset + 2 < len(data) else None
        return httpx.Response(200, json={"records": page, "next_token": nxt})


@pytest.fixture
def fw(client):
    fake = FakeWhoop()
    whoop.delete_config()
    app.state.whoop_transport = httpx.MockTransport(fake)
    yield fake
    app.state.whoop_transport = None
    whoop.delete_config()


def connect(client, fake):
    response = client.post("/api/settings/whoop", json={"client_id": "client-abc", "client_secret": "secret-xyz"})
    assert response.status_code == 200
    url = urlparse(response.json()["url"])
    query = parse_qs(url.query)
    assert url.netloc == "api.prod.whoop.com" and query["redirect_uri"] == ["https://training.example.org/api/whoop/callback"]
    assert "offline" in query["scope"][0].split() and len(query["state"][0]) == 8
    return query["state"][0]


def test_connect_stores_tokens_privately_and_never_shows_them(client, fw, monkeypatch):
    monkeypatch.setattr("app.main.threading.Thread", lambda **_: type("T", (), {"start": lambda self: None})())
    state = connect(client, fw)
    client.cookies.clear()  # der Rückweg kommt ohne Sitzungs-Cookie an
    page = client.get(f"/api/whoop/callback?state={state}&code=good-code", follow_redirects=False)
    assert page.status_code == 200 and "verbunden" in page.text
    assert whoop.connected() and stat.S_IMODE(whoop.config_path().stat().st_mode) == 0o600
    # der state gilt nur einmal
    assert client.get(f"/api/whoop/callback?state={state}&code=good-code").status_code == 400


def test_callback_rejects_wrong_or_old_state(client, fw):
    connect(client, fw)
    assert client.get("/api/whoop/callback?state=falsch12&code=good-code").status_code == 400
    assert not whoop.connected()
    state = connect(client, fw)
    conn = db.connect()
    db.set_setting(conn, "whoop_oauth_state", f"{state}|{time.time() - 3600}")
    conn.commit()
    conn.close()
    assert client.get(f"/api/whoop/callback?state={state}&code=good-code").status_code == 400
    assert not whoop.connected()


def test_callback_with_bad_code_or_denial(client, fw):
    state = connect(client, fw)
    assert client.get(f"/api/whoop/callback?state={state}&code=bad").status_code == 400
    state = connect(client, fw)
    assert client.get(f"/api/whoop/callback?state={state}&error=access_denied").status_code == 400
    assert not whoop.connected()


def test_secrets_not_in_settings(client, fw, monkeypatch):
    monkeypatch.setattr("app.main.threading.Thread", lambda **_: type("T", (), {"start": lambda self: None})())
    state = connect(client, fw)
    client.get(f"/api/whoop/callback?state={state}&code=good-code")
    text = client.get("/api/settings").text
    assert "secret-xyz" not in text and fw.refresh not in text and fw.access not in text
    s = client.get("/api/settings").json()
    assert s["whoop_connected"] and s["whoop_client_id"] == "client-abc"
    # neue Verbindung ohne Secret nutzt das gespeicherte
    assert client.post("/api/settings/whoop", json={"client_id": "client-abc"}).status_code == 200


def tokens(fake):
    whoop.save_config({"client_id": "client-abc", "client_secret": "secret-xyz",
                       "refresh_token": fake.refresh, "access_token": "", "expires_at": 0})


def test_sync_maps_days_and_skips_naps_pending_and_calibrating(client, fw):
    tokens(fw)
    whoop.run_sync(app.state.whoop_transport)
    conn = db.connect()
    rows = {r["date"]: dict(r) for r in conn.execute("SELECT * FROM wellness")}
    day = rows["2026-11-06"]
    assert day["whoop_recovery"] == 72 and day["whoop_hrv"] == 81.5 and day["whoop_resting_hr"] == 46
    assert day["whoop_sleep_s"] == 6 * 3600 and day["whoop_sleep_performance"] == 88 and day["whoop_strain"] == 12.4
    assert "2026-11-07" not in rows or rows["2026-11-07"]["whoop_recovery"] is None
    assert db.get_setting(conn, "whoop_error") == ""
    conn.close()


def test_refresh_token_rotation_is_saved(client, fw):
    tokens(fw)
    whoop.run_sync(app.state.whoop_transport)
    assert whoop.read_config()["refresh_token"] == fw.refresh == "r1"
    fw.access = "anders"  # Server hat den Access-Token verworfen
    whoop.run_sync(app.state.whoop_transport)
    assert whoop.read_config()["refresh_token"] == fw.refresh == "r2"


def test_revoked_access_becomes_warning(client, fw):
    tokens(fw)
    fw.refresh = "weg"
    whoop.run_sync(app.state.whoop_transport)
    conn = db.connect()
    assert "neu" in db.get_setting(conn, "whoop_error")
    assert any("WHOOP" in a["text"] for a in health.alerts(conn))
    conn.close()


def test_disconnect_revokes_and_deletes(client, fw):
    tokens(fw)
    assert client.delete("/api/settings/whoop").status_code == 200
    assert fw.revoked and not whoop.connected() and not whoop.config_path().exists()


def test_today_prefers_whoop(client, fw):
    today = dt.date.today().isoformat()
    conn = db.connect()
    conn.execute("INSERT INTO wellness(date,hrv,resting_hr,whoop_recovery,whoop_hrv,whoop_resting_hr) VALUES(?,?,?,?,?,?)",
                 (today, 50, 48, 64, 90, 45))
    conn.commit()
    conn.close()
    recovery = client.get("/api/today").json()["recovery"]
    assert recovery["source"] == "whoop" and recovery["recovery"]["value"] == 64
    assert recovery["hrv"]["value"] == 90 and recovery["resting_hr"]["value"] == 45


def test_local_date_uses_offset():
    assert whoop.local_date("2026-11-06T23:30:00.000Z", "+08:00") == "2026-11-07"
    assert whoop.local_date("2026-11-06T23:30:00.000Z", "-05:00") == "2026-11-06"
    assert whoop.local_date("kaputt", "+01:00") is None
