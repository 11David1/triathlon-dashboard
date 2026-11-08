import datetime as dt
import json
import subprocess

import pytest

from app import coach, db, garmin, health, trainer, worker

TODAY = dt.date(2026, 11, 7)


def new(client, **extra):
    body = {"name": "Achillessehne links", "started_on": "2026-11-01", "cause": "Umfang zu schnell", "text": "Morgens steif", **extra}
    response = client.post("/api/complaints", json=body)
    assert response.status_code == 201
    return response.json()["id"]


def get(client, complaint_id):
    return next(c for c in client.get("/api/complaints").json() if c["id"] == complaint_id)


def test_diary_grows_in_the_same_complaint(client):
    cid = new(client)
    assert client.post(f"/api/complaints/{cid}/entries", json={"text": "Dienstag schlimmer", "severity": 6}).status_code == 201
    assert client.post(f"/api/complaints/{cid}/entries", json={"text": "Freitag besser"}).status_code == 201
    complaint = get(client, cid)
    assert [e["text"] for e in complaint["entries"]] == ["Freitag besser", "Dienstag schlimmer", "Morgens steif"]
    assert complaint["entries"][1]["severity"] == 6 and complaint["entries"][0]["severity"] is None
    assert complaint["entries"][0]["date"] == dt.date.today().isoformat()


def test_resolve_keeps_everything_and_can_reopen(client):
    cid = new(client)
    assert client.post(f"/api/complaints/{cid}/resolve").status_code == 200
    complaint = get(client, cid)
    assert complaint["resolved_on"] == dt.date.today().isoformat()
    assert complaint["cause"] == "Umfang zu schnell" and len(complaint["entries"]) == 1
    assert client.post(f"/api/complaints/{cid}/reopen").status_code == 200
    assert get(client, cid)["resolved_on"] is None


def test_active_complaints_come_first(client):
    old = new(client, name="Knie")
    client.post(f"/api/complaints/{old}/resolve")
    new(client, name="Schulter", started_on="2026-10-01")
    assert [c["name"] for c in client.get("/api/complaints").json()] == ["Schulter", "Knie"]


def test_trainer_and_analysis_know_current_and_past(client):
    now = new(client, name="Achillessehne links")
    client.post(f"/api/complaints/{now}/entries", json={"text": "Freitag besser", "severity": 3})
    past = new(client, name="Knie rechts", cause="Neue Schuhe", text="Ziehen beim Treppensteigen")
    client.post(f"/api/complaints/{past}/resolve")
    conn = db.connect()
    context = coach.complaints_context(conn)
    assert [c["name"] for c in context["aktuell"]] == ["Achillessehne links"]
    assert context["aktuell"][0]["verlauf"][-1] == {"datum": dt.date.today().isoformat(), "text": "Freitag besser", "staerke_von_10": 3}
    assert context["frueher"][0]["ausgeheilt_am"] == dt.date.today().isoformat()
    assert context["frueher"][0]["vermutete_ursache"] == "Neue Schuhe"
    conn.execute("INSERT INTO activities(id,date,start_local,sport,raw) VALUES(1,'2026-11-05','2026-11-05 07:00:00','run','{}')")
    conn.commit()
    client.post("/api/chat", json={"message": "Frage"})
    prompts = [trainer.build_prompt(conn, worker.claim_chat(conn)["id"]),
               coach.build_prompt(conn, "activity", "1", TODAY), coach.build_prompt(conn, "week", "2026-11-02", TODAY)]
    conn.close()
    for prompt in prompts:
        for needle in ("Achillessehne links", "Freitag besser", "Knie rechts", "ausgeheilt_am", "Neue Schuhe"):
            assert needle in prompt
    assert "Frühere sind" in trainer.SYSTEM and "Frühere sind" in coach.SYSTEM


def test_complaint_validation(client):
    future = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    assert client.post("/api/complaints", json={"name": "x", "started_on": future}).status_code == 400
    assert client.post("/api/complaints", json={"name": "", "started_on": "2026-11-01"}).status_code == 422
    cid = new(client)
    assert client.post(f"/api/complaints/{cid}/entries", json={"text": "   "}).status_code == 400
    assert client.post(f"/api/complaints/{cid}/entries", json={"text": "x", "severity": 11}).status_code == 422
    assert client.post("/api/complaints/999/entries", json={"text": "x"}).status_code == 404
    assert client.post("/api/complaints/999/resolve").status_code == 404
    assert client.put("/api/complaints/999", json={"name": "x", "started_on": "2026-11-01"}).status_code == 404


def test_edit_and_delete(client):
    cid = new(client)
    client.put(f"/api/complaints/{cid}", json={"name": "Wade", "started_on": "2026-10-30", "cause": "Tennis"})
    complaint = get(client, cid)
    assert (complaint["name"], complaint["cause"]) == ("Wade", "Tennis") and len(complaint["entries"]) == 1
    client.delete(f"/api/complaints/{cid}/entries/{complaint['entries'][0]['id']}")
    assert get(client, cid)["entries"] == []
    client.post(f"/api/complaints/{cid}/entries", json={"text": "x"})
    client.delete(f"/api/complaints/{cid}")
    assert client.get("/api/complaints").json() == []
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM complaint_entries").fetchone()["c"] == 0
    conn.close()


def test_move_plan(client):
    plan = client.post("/api/planned", json={"date": "2026-12-01", "sport": "run", "title": "A", "duration_min": 40, "notes": "bleibt"}).json()["id"]
    assert client.post(f"/api/planned/{plan}/move", json={"date": "2026-12-03"}).status_code == 200
    row = client.get("/api/calendar?start=2026-12-01&end=2026-12-07").json()["planned"][0]
    assert (row["date"], row["title"], row["duration_min"], row["notes"]) == ("2026-12-03", "A", 40, "bleibt")
    assert client.post(f"/api/planned/{plan}/move", json={"date": "kein-datum"}).status_code == 422
    assert client.post("/api/planned/999/move", json={"date": "2026-12-03"}).status_code == 404


# --- Warnungen -------------------------------------------------------------

def alert_texts(conn, **kwargs):
    return [a["text"] for a in health.alerts(conn, **kwargs)]


@pytest.fixture
def conn(anon):
    connection = db.connect()
    db.set_setting(connection, "worker_heartbeat", "1000000")
    yield connection
    connection.close()


NOW = 1000060.0


def test_no_key_asks_to_connect(conn):
    assert "Verbinde Garmin" in alert_texts(conn, now=NOW, today=TODAY)[0]


def key_state(conn, expires="2027-06-01", synced=NOW):
    garmin.save_key("good-key-123")
    db.set_setting(conn, "garmin_key_expires", expires)
    db.set_setting(conn, "last_sync_at", str(synced))


def test_all_fine_means_no_alerts(conn):
    key_state(conn)
    assert health.alerts(conn, now=NOW, today=TODAY) == []


@pytest.mark.parametrize("expires,needle", [("2026-11-21", "in 14 Tagen"), ("2026-11-08", "morgen"), ("2026-11-07", "heute"), ("2026-11-06", "abgelaufen")])
def test_key_expiry_warnings(conn, expires, needle):
    key_state(conn, expires)
    assert needle in alert_texts(conn, now=NOW, today=TODAY)[0]


def test_key_far_from_expiry_is_quiet_and_unknown_asks_once(conn):
    key_state(conn, "2026-11-22")
    assert alert_texts(conn, now=NOW, today=TODAY) == []
    key_state(conn, "")
    assert "wann dein Garmin-Key abläuft" in alert_texts(conn, now=NOW, today=TODAY)[0]


def test_token_hint_is_the_safety_net(conn):
    key_state(conn, "")
    db.set_setting(conn, "garmin_key_expiry_hint", "2026-11-08")
    assert "morgen" in alert_texts(conn, now=NOW, today=TODAY)[0]


def test_stale_sync_and_sync_error(conn):
    key_state(conn, synced=NOW - 3 * 3600)
    assert "3 Stunden" in alert_texts(conn, now=NOW, today=TODAY)[0]
    db.set_setting(conn, "last_sync_error", "Garmin lehnt den API-Key ab")
    texts = alert_texts(conn, now=NOW, today=TODAY)
    assert len(texts) == 1 and "lehnt" in texts[0]


def test_dead_worker_is_reported(conn):
    key_state(conn)
    assert "läuft gerade nicht" in alert_texts(conn, now=NOW + 10000, today=TODAY)[-1]


def test_claude_login_alerts(conn):
    key_state(conn)
    db.set_setting(conn, "claude_status", json.dumps({"logged_in": False, "expires_at": None}))
    assert "abgemeldet" in alert_texts(conn, now=NOW, today=TODAY)[0]
    db.set_setting(conn, "claude_status", json.dumps({"logged_in": True, "expires_at": "2026-11-09T01:00:00.000Z"}))
    assert "09.11.2026" in alert_texts(conn, now=NOW, today=TODAY)[0]
    db.set_setting(conn, "claude_status", json.dumps({"logged_in": True, "expires_at": "2026-12-07T01:00:00.000Z"}))
    assert alert_texts(conn, now=NOW, today=TODAY) == []


def test_auth_status_keeps_no_account_data():
    text = json.dumps({"loggedIn": True, "email": "x@example.org", "orgName": "Y", "loginExpiresAt": "2026-12-07T01:31:04.396Z"})
    assert health.parse_auth_status(text) == {"logged_in": True, "expires_at": "2026-12-07T01:31:04.396Z"}
    assert health.parse_auth_status("kaputt") is None


def test_claude_failure_shows_and_clears(client):
    conn = db.connect()
    client.post("/api/chat", json={"message": "Hallo"})

    def down(_):
        raise worker.ClaudeFailed("Claude-Aufruf fehlgeschlagen (Code 1): Limit erreicht")

    worker.process_chat(conn, worker.claim_chat(conn), runner=down)
    assert "Limit erreicht" in " ".join(a["text"] for a in client.get("/api/overview").json()["alerts"])
    message = client.get("/api/chat").json()[-1]
    client.post(f"/api/chat/{message['id']}/retry")
    worker.process_chat(conn, worker.claim_chat(conn), runner=lambda _: "Wieder da")
    assert "Limit" not in " ".join(a["text"] for a in client.get("/api/overview").json()["alerts"])
    conn.close()


def test_data_errors_are_not_blamed_on_claude(client):
    conn = db.connect()
    garmin.queue_analysis(conn, "activity", "404")
    worker.process(conn, worker.claim(conn), runner=lambda _: "x")
    assert not db.get_setting(conn, "claude_error")
    conn.close()


def test_key_days_set_expiry_and_new_key_clears_old_state(client, fake):
    conn = db.connect()
    db.set_setting(conn, "garmin_key_expiry_hint", "2026-01-01")
    db.set_setting(conn, "last_sync_error", "alt")
    conn.commit()
    client.post("/api/settings/garmin-key", json={"key": fake.good_key, "valid_days": 90})
    settings = client.get("/api/settings").json()
    assert settings["garmin_key_expires"] == (dt.date.today() + dt.timedelta(days=90)).isoformat()
    assert db.get_setting(conn, "garmin_key_expiry_hint") == ""
    conn.close()
    assert client.put("/api/settings/garmin-key-expiry", json={"expires_on": "2027-01-15"}).status_code == 200
    assert client.get("/api/settings").json()["garmin_key_expires"] == "2027-01-15"


def test_short_token_sets_hint(anon, fake):
    import base64
    import time

    import httpx
    soon = "h." + base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode()).decode().rstrip("=") + ".s"

    def transport(request):
        if request.url.path == garmin.TOKEN_PATH:
            return httpx.Response(200, text=soon)
        return httpx.Response(200, json=[])

    garmin.save_key("good-key-123")
    garmin.run_sync(httpx.MockTransport(transport))
    conn = db.connect()
    assert db.get_setting(conn, "garmin_key_expiry_hint") == dt.date.fromtimestamp(time.time() + 3600).isoformat()
    conn.close()
