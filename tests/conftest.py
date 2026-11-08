import base64
import shutil
import json
import os
import tempfile
import time

os.environ["TRI_DATA_DIR"] = tempfile.mkdtemp(prefix="tri-test-")
os.environ["TRI_SYNC_INTERVAL_S"] = "0"
os.environ["TRI_ORIGIN"] = "https://training.example.org"

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, db, garmin, gcal, users, whoop
from app.main import app, throttle

# Tests ohne Anfrage arbeiten in Annas Datenordner, wie die Web-App nach seinem Login.
db._current_dir.set(db.ROOT_DIR)

PASSWORD = "richtig-langes-passwort"
JWT = "h." + base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 86400}).encode()).decode().rstrip("=") + ".s"


class FakeGarmin:
    """Spielt Garmins Personal API nach."""

    def __init__(self):
        self.calls = []
        self.good_key = "good-key-123"
        self.activities = []
        self.expire_next_bearer = False
        self.details = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == garmin.TOKEN_PATH:
            if request.headers.get("authorization") != f"ApiKey {self.good_key}":
                return httpx.Response(401, text="nope")
            return httpx.Response(200, text=JWT)
        if self.expire_next_bearer:
            self.expire_next_bearer = False
            return httpx.Response(401)
        if request.headers.get("authorization") != f"Bearer {JWT}":
            return httpx.Response(401)
        if path.endswith("userprofile/settings"):
            return httpx.Response(200, json={})
        if path.endswith("search/activities"):
            start = int(request.url.params["start"])
            return httpx.Response(200, json=self.activities[start:start + int(request.url.params["limit"])])
        if path.startswith("/activity-service/activity/"):
            return httpx.Response(200, json=self.details.get(path.rsplit("/", 1)[1], {}))
        if path.endswith("dailySleepSummariesByDate"):
            return httpx.Response(200, json=[{"calendarDate": "2026-11-05", "sleepTimeSeconds": 27000,
                                              "sleepScores": {"overall": {"value": 81}}}])
        if path.startswith("/hrv-service/hrv/daily"):
            return httpx.Response(200, json={"hrvSummaries": [{"calendarDate": "2026-11-05", "lastNightAvg": 58}]})
        if path.endswith("v2/dailysummaries"):
            return httpx.Response(200, json={"data": [{"calendarDate": "2026-11-05", "heartRate": {"restingValue": 47}}]})
        return httpx.Response(404)


@pytest.fixture
def fake():
    return FakeGarmin()


@pytest.fixture
def anon(fake):
    shutil.rmtree(db.ROOT_DIR / "users", ignore_errors=True)
    users.init()
    db.init()
    accounts = users.connect()
    accounts.execute("DELETE FROM users")
    accounts.execute("DELETE FROM sessions")
    accounts.execute("INSERT INTO users(name,display_name,dir,password_hash,created_at,claude) VALUES('anna','Anna','.',?,0,'abo')",
                     (auth.hash_password(PASSWORD),))
    accounts.commit()
    accounts.close()
    conn = db.connect()
    for table in ("settings", "planned", "activities", "wellness", "analyses", "chat_messages", "trainer_knowledge", "complaints", "complaint_entries", "calendar_events"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    garmin.delete_key()
    gcal.delete_urls()
    whoop.delete_config()
    throttle._fails.clear()
    app.state.garmin_transport = httpx.MockTransport(fake)
    app.state.calendar_transport = None
    with TestClient(app, base_url="https://training.example.org") as client:
        yield client


@pytest.fixture
def client(anon):
    assert anon.post("/api/login", json={"username": "anna", "password": PASSWORD}).status_code == 200
    return anon
