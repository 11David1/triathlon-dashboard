import datetime as dt
import stat

from app import db, garmin


def row(activity_id, date="2026-11-05", kind="running"):
    return {"activityId": activity_id, "activityName": "Lauf", "startTimeLocal": f"{date} 07:30:00",
            "activityType": {"typeKey": kind}, "duration": 3600.5, "distance": 12000.0, "averageHR": 142,
            "maxHR": 171, "averageSpeed": 3.33, "startLatitude": 51.43, "ownerFullName": "Geheim"}


def test_bad_key_is_not_saved(client):
    response = client.post("/api/settings/garmin-key", json={"key": "wrong-key-000"})
    assert response.status_code == 400
    assert garmin.read_key() is None


def test_good_key_saved_private_and_never_returned(client, fake):
    assert client.post("/api/settings/garmin-key", json={"key": fake.good_key}).status_code == 200
    assert stat.S_IMODE(garmin.key_path().stat().st_mode) == 0o600
    assert client.get("/api/settings").json()["garmin_key_set"] is True
    for path in ("/api/settings", "/api/overview"):
        assert fake.good_key not in client.get(path).text


def sync(fake, today=dt.date(2026, 11, 7)):
    conn = db.connect()
    c = garmin.Client(fake.good_key, __import__("httpx").MockTransport(fake))
    try:
        return conn, garmin.sync(conn, c, today)
    finally:
        c.close()


def test_sync_stores_and_normalizes(anon, fake):
    fake.activities = [row(1)]
    fake.details = {"1": {"lapDTOs": [{"distance": 1000, "duration": 300, "averageHR": 140}]}}
    conn, result = sync(fake)
    assert result["new"] == 1
    saved = dict(conn.execute("SELECT * FROM activities WHERE id=1").fetchone())
    assert saved["sport"] == "run" and saved["date"] == "2026-11-05" and saved["avg_hr"] == 142
    assert garmin.laps(saved["details"])[0]["distance_m"] == 1000
    well = dict(conn.execute("SELECT * FROM wellness WHERE date='2026-11-05'").fetchone())
    assert (well["sleep_score"], well["hrv"], well["resting_hr"]) == (81, 58, 47)
    conn.close()


def test_sync_paginates(anon, fake):
    fake.activities = [row(i) for i in range(1, 251)]
    conn, result = sync(fake)
    assert result["new"] == 250
    conn.close()


def test_resync_updates_without_duplicates_or_new_analysis(anon, fake):
    fake.activities = [row(1)]
    conn, first = sync(fake)
    assert conn.execute("SELECT COUNT(*) c FROM analyses").fetchone()["c"] == 1
    fake.activities = [{**row(1), "activityName": "Umbenannt"}]
    _, second = sync(fake)
    assert second["new"] == 0
    assert conn.execute("SELECT name FROM activities").fetchone()["name"] == "Umbenannt"
    assert conn.execute("SELECT COUNT(*) c FROM analyses").fetchone()["c"] == 1
    conn.close()


def test_only_fresh_activities_are_auto_analyzed(anon, fake):
    fake.activities = [row(1, "2026-08-01"), row(2, "2026-11-06")]
    conn, _ = sync(fake)
    refs = [r["ref"] for r in conn.execute("SELECT ref FROM analyses")]
    assert refs == ["2"]
    conn.close()


def test_bearer_refresh_on_401(anon, fake):
    client = garmin.Client(fake.good_key, __import__("httpx").MockTransport(fake))
    client.verify()
    fake.expire_next_bearer = True
    client.verify()
    assert fake.calls.count(garmin.TOKEN_PATH) == 2
    client.close()


def test_expired_key_reported_in_status(client, fake):
    garmin.save_key("expired-key-1")
    assert client.post("/api/sync").status_code == 200
    error = client.get("/api/overview").json()["garmin"]["error"]
    assert "API-Key" in error and "expired-key-1" not in error


def test_missing_wellness_does_not_stop_sync(anon, fake):
    original = fake.__call__
    fake.activities = [row(1)]

    class Partial:
        def __call__(self, request):
            import httpx
            if "hrv-service" in request.url.path:
                return httpx.Response(404)
            return original(request)

    import httpx
    conn = db.connect()
    c = garmin.Client(fake.good_key, httpx.MockTransport(Partial()))
    result = garmin.sync(conn, c, dt.date(2026, 11, 7))
    assert result["new"] == 1 and any("HRV" in p for p in result["problems"])
    c.close()
    conn.close()


def test_malformed_rows_are_skipped():
    assert garmin.normalize_activity({"activityName": "ohne id"}) is None
    assert garmin.normalize_activity({"activityId": 1}) is None
    assert garmin.laps(None) == []
    assert garmin.laps('{"a": 1}') == []


def test_real_garmin_daily_summary_shape_is_read(anon, fake):
    import httpx
    original = fake.__call__

    class Nested:
        def __call__(self, request):
            if request.url.path.endswith("v2/dailysummaries"):
                return httpx.Response(200, json={"data": [{"calendarDate": "2026-11-05",
                    "heartRate": {"restingValue": 46}, "bodyBattery": {"maxValue": 88}}]})
            return original(request)

    conn = db.connect()
    c = garmin.Client(fake.good_key, httpx.MockTransport(Nested()))
    garmin.sync(conn, c, dt.date(2026, 11, 7))
    row = conn.execute("SELECT resting_hr,body_battery FROM wellness WHERE date='2026-11-05'").fetchone()
    assert (row["resting_hr"], row["body_battery"]) == (46, 88)
    c.close()
    conn.close()


def test_new_wellness_version_backfills_once(anon, fake):
    conn, _ = sync(fake)
    db.set_setting(conn, "wellness_version", "1")
    conn.commit()
    fake.calls.clear()
    sync(fake)
    assert sum("/hrv-service/hrv/daily" in p for p in fake.calls) >= 2  # 60 Tage in 28-Tage-Fenstern
    fake.calls.clear()
    sync(fake)
    assert sum("/hrv-service/hrv/daily" in p for p in fake.calls) == 1
    conn.close()


def test_split_summaries_are_labelled():
    import json
    rows = garmin.laps(json.dumps({"splitSummaries": [{"splitType": "RWD_WALK", "distance": 500, "duration": 300}]}))
    assert rows[0]["label"] == "Gehen"
