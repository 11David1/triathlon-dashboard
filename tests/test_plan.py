from app.plan import match, normalize_sport


def test_plan_crud(client):
    created = client.post("/api/planned", json={"date": "2026-12-01", "sport": "run", "title": "Locker", "duration_min": 45})
    assert created.status_code == 201
    plan_id = created.json()["id"]
    rows = client.get("/api/calendar?start=2026-12-01&end=2026-12-07").json()["planned"]
    assert rows[0]["title"] == "Locker"
    assert client.put(f"/api/planned/{plan_id}", json={"date": "2026-12-02", "sport": "bike", "title": "Neu"}).status_code == 200
    rows = client.get("/api/calendar?start=2026-12-01&end=2026-12-07").json()["planned"]
    assert (rows[0]["date"], rows[0]["sport"]) == ("2026-12-02", "bike")
    assert client.delete(f"/api/planned/{plan_id}").status_code == 200
    assert client.get("/api/calendar?start=2026-12-01&end=2026-12-07").json()["planned"] == []


def test_plan_validation(client):
    assert client.post("/api/planned", json={"date": "kein-datum", "sport": "run"}).status_code == 422
    assert client.post("/api/planned", json={"date": "2026-12-01", "sport": "golf"}).status_code == 422
    assert client.put("/api/planned/999", json={"date": "2026-12-01", "sport": "run"}).status_code == 404


def test_calendar_range_limited(client):
    assert client.get("/api/calendar?start=2026-01-01&end=2026-12-31").status_code == 400


def test_sport_mapping():
    assert normalize_sport("lap_swimming") == "swim"
    assert normalize_sport("open_water_swimming") == "swim"
    assert normalize_sport("road_biking") == "bike"
    assert normalize_sport("indoor_cycling") == "bike"
    assert normalize_sport("virtual_ride") == "bike"
    assert normalize_sport("trail_running") == "run"
    assert normalize_sport("treadmill_running") == "run"
    assert normalize_sport("strength_training") == "strength"
    assert normalize_sport("walking") == "other"
    assert normalize_sport(None) == "other"


def plan(id, date, sport, minutes=60):
    return {"id": id, "date": date, "sport": sport, "duration_min": minutes}


def act(id, date, sport, seconds=3600):
    return {"id": id, "date": date, "sport": sport, "duration_s": seconds}


def test_match_statuses():
    result = match(
        [plan(1, "2026-11-02", "run"), plan(2, "2026-11-03", "bike"), plan(3, "2026-11-09", "swim"), plan(4, "2026-11-04", "rest")],
        [act(10, "2026-11-02", "run")], today="2026-11-07")
    assert result[1] == {"status": "done", "activity_ids": [10]}
    assert result[2]["status"] == "missed"
    assert result[3]["status"] == "open"
    assert result[4]["status"] == "rest"


def test_match_uses_each_activity_once_and_closest_duration():
    result = match(
        [plan(1, "2026-11-02", "run", 30), plan(2, "2026-11-02", "run", 90)],
        [act(10, "2026-11-02", "run", 5400), act(11, "2026-11-02", "run", 1800)], today="2026-11-07")
    assert result[1]["activity_ids"] == [11]
    assert result[2]["activity_ids"] == [10]


def test_brick_needs_bike_and_run():
    one = match([plan(1, "2026-11-02", "brick")], [act(10, "2026-11-02", "bike")], today="2026-11-07")
    assert one[1]["status"] == "partial"
    both = match([plan(1, "2026-11-02", "brick")], [act(10, "2026-11-02", "bike"), act(11, "2026-11-02", "run")], today="2026-11-07")
    assert both[1]["status"] == "done"
