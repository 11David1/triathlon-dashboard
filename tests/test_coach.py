import datetime as dt
import json
import stat
import subprocess
import time

import pytest

from app import coach, db, garmin, worker

TODAY = dt.date(2026, 11, 7)


def insert_activity(conn, activity_id=1, date="2026-11-05", raw=None, details=None):
    raw = raw if raw is not None else {"activityName": "Morgenlauf", "startLatitude": 51.43, "startLongitude": 6.76,
                                       "ownerFullName": "Geheim", "deviceId": 99, "averageHR": 142}
    conn.execute(
        "INSERT INTO activities(id,date,start_local,sport,name,duration_s,distance_m,raw,details) VALUES(?,?,?,?,?,?,?,?,?)",
        (activity_id, date, f"{date} 07:30:00", "run", "Morgenlauf", 3600, 12000, json.dumps(raw),
         json.dumps(details) if details else None))
    conn.commit()


def test_activity_prompt_has_data_but_no_location_or_identity(anon):
    conn = db.connect()
    insert_activity(conn, details={"geoPolylineDTO": {"polyline": [1, 2]}, "summaryDTO": {"startLatitude": 51.43, "maxHR": 171}})
    conn.execute("INSERT INTO planned(date,sport,title,duration_min) VALUES('2026-11-05','run','Dauerlauf',60)")
    conn.execute("INSERT INTO wellness(date,hrv) VALUES('2026-11-05',58)")
    conn.commit()
    prompt = coach.build_prompt(conn, "activity", "1", TODAY)
    for needle in ("Morgenlauf", "Dauerlauf", "tage_bis_rennen", '"hrv": 58', '"maxHR": 171', "<daten>"):
        assert needle in prompt
    for secret in ("51.43", "6.76", "Geheim", "polyline", '"deviceId"'):
        assert secret not in prompt
    conn.close()


def test_week_prompt(anon):
    conn = db.connect()
    insert_activity(conn, date="2026-11-03")
    conn.execute("INSERT INTO planned(date,sport,title) VALUES('2026-11-04','swim','Technik')")
    conn.commit()
    prompt = coach.build_prompt(conn, "week", "2026-11-02", TODAY)
    assert "Technik" in prompt and '"status": "missed"' in prompt and "Morgenlauf" in prompt
    conn.close()


def test_long_details_are_capped(anon):
    conn = db.connect()
    insert_activity(conn, details={"x": ["a" * 500] * 80})
    prompt = coach.build_prompt(conn, "activity", "1", TODAY)
    assert "(gekürzt)" in prompt and len(prompt) < coach.MAX_DETAIL_CHARS + 6000
    conn.close()


def test_claude_runs_without_tools():
    command = worker.claude_command()
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command and "--setting-sources" in command


def queue(conn, scope="activity", ref="1"):
    garmin.queue_analysis(conn, scope, ref)
    return worker.claim(conn)


def test_worker_success(anon):
    conn = db.connect()
    insert_activity(conn)
    row = queue(conn)
    assert row["status"] == "pending"
    assert conn.execute("SELECT status FROM analyses").fetchone()["status"] == "running"
    worker.process(conn, row, runner=lambda prompt: "**Kurzfazit**\nGut.")
    saved = conn.execute("SELECT * FROM analyses").fetchone()
    assert saved["status"] == "done" and "Kurzfazit" in saved["result"]
    conn.close()


def test_worker_records_failures(anon):
    conn = db.connect()
    insert_activity(conn)

    def boom(_):
        raise RuntimeError("Login abgelaufen")

    worker.process(conn, queue(conn), runner=boom)
    saved = conn.execute("SELECT * FROM analyses").fetchone()
    assert saved["status"] == "failed" and "Login abgelaufen" in saved["error"]

    def slow(_):
        raise subprocess.TimeoutExpired("claude", 1)

    worker.process(conn, queue(conn), runner=slow)
    assert "Zeitüberschreitung" in conn.execute("SELECT error FROM analyses ORDER BY id DESC").fetchone()["error"]
    conn.close()


def test_worker_handles_deleted_activity(anon):
    conn = db.connect()
    worker.process(conn, queue(conn, ref="404"), runner=lambda _: "x")
    assert conn.execute("SELECT status FROM analyses").fetchone()["status"] == "failed"
    conn.close()


def test_duplicate_requests_share_one_job(client):
    conn = db.connect()
    insert_activity(conn)
    conn.close()
    first = client.post("/api/analyses", json={"scope": "activity", "ref": "1"}).json()["id"]
    second = client.post("/api/analyses", json={"scope": "activity", "ref": "1"}).json()["id"]
    assert first == second


def test_analysis_request_validation(client):
    assert client.post("/api/analyses", json={"scope": "activity", "ref": "999"}).status_code == 404
    assert client.post("/api/analyses", json={"scope": "activity", "ref": "1;rm"}).status_code == 404
    assert client.post("/api/analyses", json={"scope": "week", "ref": "kein-datum"}).status_code == 400
    assert client.post("/api/analyses", json={"scope": "shell", "ref": "1"}).status_code == 422


def test_settings_and_overview(client):
    assert client.put("/api/settings", json={"race_name": "Rennen", "race_date": "2027-08-29", "athlete_notes": "FTP 250"}).status_code == 200
    overview = client.get("/api/overview").json()
    assert overview["race"]["name"] == "Rennen"
    assert overview["race"]["days_left"] == (dt.date(2027, 8, 29) - dt.date.today()).days


def test_trends_groups_by_week(client):
    conn = db.connect()
    today = dt.date.today()
    insert_activity(conn, date=today.isoformat())
    conn.close()
    weeks = client.get("/api/trends").json()["weeks"]
    assert len(weeks) == 12 and weeks[-1]["run"] == 1.0 and weeks[0]["run"] == 0


def manual(**extra):
    return {"date": "2026-11-01", "time": "07:15", "sport": "swim", "name": "See", "duration_min": 90,
            "distance_km": 3, "avg_hr": 130, "note": "Arme schwer, kaum geschlafen", **extra}


def test_manual_training_is_stored_and_auto_analyzed(client):
    created = client.post("/api/activities", json=manual())
    assert created.status_code == 201
    new_id = created.json()["id"]
    assert new_id < 0
    detail = client.get(f"/api/activities/{new_id}").json()
    assert detail["duration_s"] == 5400 and detail["distance_m"] == 3000
    assert round(detail["avg_speed"], 3) == 0.556 and detail["note"].startswith("Arme")
    assert detail["analysis"]["status"] == "pending"
    assert client.get("/api/activities").json()[0]["id"] == new_id
    assert client.post("/api/activities", json=manual()).json()["id"] == new_id - 1


def test_manual_training_counts_for_plan_and_prompt(client):
    client.post("/api/planned", json={"date": "2026-11-01", "sport": "swim", "title": "Technik", "duration_min": 60})
    new_id = client.post("/api/activities", json=manual()).json()["id"]
    plans = client.get("/api/calendar?start=2026-11-01&end=2026-11-01").json()["planned"]
    assert plans[0]["status"] == "done" and plans[0]["activity_ids"] == [new_id]
    conn = db.connect()
    prompt = coach.build_prompt(conn, "activity", str(new_id), TODAY)
    conn.close()
    for needle in ("Arme schwer", "eigene_notiz", "manuell eingetragen", "Technik"):
        assert needle in prompt


def test_analysis_can_be_requested_for_manual_training(client):
    new_id = client.post("/api/activities", json=manual()).json()["id"]
    assert client.post("/api/analyses", json={"scope": "activity", "ref": str(new_id)}).status_code == 202


def test_manual_validation(client):
    future = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    assert client.post("/api/activities", json=manual(date=future)).status_code == 400
    assert client.post("/api/activities", json=manual(duration_min=0)).status_code == 422
    assert client.post("/api/activities", json=manual(time="25:00")).status_code == 422
    assert client.post("/api/activities", json=manual(sport="brick")).status_code == 422


def test_manual_edit_and_delete(client):
    new_id = client.post("/api/activities", json=manual()).json()["id"]
    assert client.put(f"/api/activities/{new_id}", json=manual(name="Neu", duration_min=30)).status_code == 200
    assert client.get(f"/api/activities/{new_id}").json()["name"] == "Neu"
    assert client.delete(f"/api/activities/{new_id}").status_code == 200
    assert client.get(f"/api/activities/{new_id}").status_code == 404
    assert client.get("/api/analyses/latest", params={"scope": "activity", "ref": str(new_id)}).json() is None


def test_garmin_training_is_protected_but_note_works(client):
    conn = db.connect()
    insert_activity(conn)
    conn.close()
    assert client.put("/api/activities/1", json=manual()).status_code == 403
    assert client.delete("/api/activities/1").status_code == 403
    assert client.put("/api/activities/1/note", json={"note": "Wade zwickt"}).status_code == 200
    assert client.get("/api/activities/1").json()["note"] == "Wade zwickt"
    assert client.put("/api/activities/999/note", json={"note": "x"}).status_code == 404
    assert client.put("/api/activities/1/note", json={"note": "x" * 4001}).status_code == 422


def test_note_survives_sync(anon, fake):
    conn = db.connect()
    fake.activities = [{"activityId": 1, "activityName": "A", "startTimeLocal": "2026-11-05 07:30:00",
                        "activityType": {"typeKey": "running"}, "duration": 60}]
    c = garmin.Client(fake.good_key, __import__("httpx").MockTransport(fake))
    garmin.sync(conn, c, TODAY)
    conn.execute("UPDATE activities SET note='bleibt' WHERE id=1")
    conn.commit()
    garmin.sync(conn, c, TODAY)
    assert conn.execute("SELECT note FROM activities").fetchone()["note"] == "bleibt"
    c.close()
    conn.close()


def test_sync_never_asks_garmin_about_manual_trainings(client, fake):
    client.post("/api/activities", json=manual(date=dt.date.today().isoformat()))
    conn = db.connect()
    c = garmin.Client(fake.good_key, __import__("httpx").MockTransport(fake))
    garmin.sync(conn, c)
    c.close()
    conn.close()
    assert not [p for p in fake.calls if "/activity/-" in p]


def test_filter_keeps_metrics_and_drops_identity():
    kept = coach.clean({"avgVerticalOscillation": 8.1, "relativeEffort": 3, "startLatitude": 51.4, "endLongitude": 6.7,
                        "lat": 1, "locationName": "Musterstadt", "metadataDTO": {"userInfoDto": {"fullname": "X"}, "lapCount": 4},
                        "sensors": [{"serialNumber": 1}], "ownerFullName": "X"})
    assert kept == {"avgVerticalOscillation": 8.1, "relativeEffort": 3, "metadataDTO": {"lapCount": 4}}


def test_trends_has_distance_elevation_and_count(client):
    today = dt.date.today().isoformat()
    client.post("/api/activities", json=manual(date=today, sport="bike", distance_km=40, elevation_m=300))
    week = client.get("/api/trends?weeks=52").json()["weeks"]
    assert len(week) == 52
    assert week[-1]["km"]["bike"] == 40 and week[-1]["hm"]["bike"] == 300 and week[-1]["count"]["bike"] == 1
    assert week[-1]["bike"] == 1.5


def test_place_name_is_removed_from_activity_title(anon):
    conn = db.connect()
    raw = {"activityName": "Musterstadt Laufen", "locationName": "Musterstadt", "averageHR": 140}
    conn.execute("INSERT INTO activities(id,date,start_local,sport,name,raw,details) VALUES(5,'2026-11-05','2026-11-05 07:00:00','run',?,?,?)",
                 ("Musterstadt Laufen", json.dumps(raw), json.dumps({"activityName": "Musterstadt Laufen", "locationName": "Musterstadt"})))
    conn.commit()
    for scope, ref in (("activity", "5"), ("week", "2026-11-02")):
        prompt = coach.build_prompt(conn, scope, ref, TODAY)
        assert "Musterstadt" not in prompt and '"name": "Laufen"' in prompt
    conn.close()
