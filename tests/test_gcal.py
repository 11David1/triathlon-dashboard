import datetime as dt
import stat

import httpx
import pytest

from app import coach, db, gcal, health, trainer, worker
from app.main import app
from app.plan import classify_title

TODAY = dt.date(2026, 11, 9)  # Montag
SECRET = "https://calendar.google.com/calendar/ical/ich%40example.org/private-0123abcdef/basic.ics"

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//DE
BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:utc
DTSTART:20261110T170000Z
DTEND:20261110T200000Z
SUMMARY:Konzert
DESCRIPTION:Geheime Beschreibung
LOCATION:Geheime Adresse 5
ATTENDEE:mailto:freund@example.org
END:VEVENT
BEGIN:VEVENT
UID:weekly
DTSTART;TZID=Europe/Berlin:20261012T180000
DTEND;TZID=Europe/Berlin:20261012T190000
RRULE:FREQ=WEEKLY;COUNT=10
SUMMARY:Kurs jeden Montag
END:VEVENT
BEGIN:VEVENT
UID:allday
DTSTART;VALUE=DATE:20261113
DTEND;VALUE=DATE:20261116
SUMMARY:Kurzurlaub
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTART:20261111T100000Z
DTEND:20261111T110000Z
STATUS:CANCELLED
SUMMARY:Abgesagt
END:VEVENT
BEGIN:VEVENT
UID:free
DTSTART:20261112T100000Z
DTEND:20261112T110000Z
TRANSP:TRANSPARENT
SUMMARY:Nur zur Info
END:VEVENT
BEGIN:VEVENT
UID:old
DTSTART:20200101T100000Z
DTEND:20200101T110000Z
SUMMARY:Lange her
END:VEVENT
END:VCALENDAR
"""


def transport(body=ICS, status=200):
    return httpx.MockTransport(lambda request: httpx.Response(status, content=body))


def by_title(events):
    """Je Titel der erste Termin, bei Serien also der nächste."""
    found = {}
    for event in events:
        found.setdefault(event["title"], event)
    return found


def test_parse_times_series_all_day_and_filters():
    events = by_title(gcal.parse_events(ICS, TODAY))
    assert events["Konzert"]["start"] == "2026-11-10T18:00" and events["Konzert"]["end"] == "2026-11-10T21:00"
    assert events["Kurs jeden Montag"]["start"] == "2026-11-09T18:00"
    assert events["Kurzurlaub"]["start"] == "2026-11-13" and events["Kurzurlaub"]["end"] == "2026-11-15"
    assert events["Kurzurlaub"]["all_day"] == 1
    assert events["Nur zur Info"]["free"] == 1
    assert "Abgesagt" not in events and "Lange her" not in events


def test_series_expands_each_week():
    weekly = [e for e in gcal.parse_events(ICS, TODAY) if e["title"] == "Kurs jeden Montag"]
    assert [e["start"][:10] for e in weekly] == ["2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30", "2026-12-07", "2026-12-14"]


def test_only_titles_and_times_are_kept():
    text = str(gcal.parse_events(ICS, TODAY))
    for secret in ("Geheime Beschreibung", "Geheime Adresse", "freund@example.org"):
        assert secret not in text


def test_garbage_is_reported_not_raised_raw():
    with pytest.raises(gcal.CalendarError):
        gcal.parse_events(b"kein kalender", TODAY)


@pytest.mark.parametrize("url", [
    "http://calendar.google.com/calendar/ical/a/private-12ab/basic.ics",
    "https://evil.example/calendar/ical/a/private-12ab/basic.ics",
    "https://calendar.google.com.evil.example/calendar/ical/a/private-12ab/basic.ics",
    "https://calendar.google.com/calendar/embed?src=a",
    "http://169.254.169.254/latest",
    "https://calendar.google.com/calendar/ical/a/private-12ab/basic.ics?x=/../",
])
def test_only_google_ical_addresses_are_accepted(url):
    assert not gcal.valid_url(url)


def test_real_formats_are_accepted():
    assert gcal.valid_url(SECRET)
    assert gcal.valid_url("https://calendar.google.com/calendar/ical/de.german%23holiday%40group.v.calendar.google.com/public/basic.ics")


def connect(client):
    app.state.calendar_transport = transport()
    return client.post("/api/settings/calendar", json={"urls": SECRET})


def test_connect_stores_secret_privately_and_never_returns_it(client):
    response = connect(client)
    assert response.status_code == 200 and response.json()["calendars"] == 1
    assert stat.S_IMODE(gcal.urls_path().stat().st_mode) == 0o600
    settings = client.get("/api/settings")
    assert settings.json()["calendar_count"] == 1
    for path in ("/api/settings", "/api/overview"):
        assert "private-0123abcdef" not in client.get(path).text


def test_bad_address_is_not_saved_and_not_fetched(client):
    calls = []
    app.state.calendar_transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, content=ICS))
    assert client.post("/api/settings/calendar", json={"urls": "https://evil.example/x.ics"}).status_code == 400
    assert calls == [] and gcal.read_urls() == []


def test_rejected_address_is_not_saved(client):
    app.state.calendar_transport = transport(status=404)
    response = client.post("/api/settings/calendar", json={"urls": SECRET})
    assert response.status_code == 400 and "private-0123abcdef" not in response.text
    assert gcal.read_urls() == []


def test_too_many_addresses(client):
    urls = "\n".join(SECRET.replace("0123abcdef", f"{i:04x}") for i in range(6))
    assert client.post("/api/settings/calendar", json={"urls": urls}).status_code == 400


def ask(client):
    client.post("/api/chat", json={"message": "Plane meine Woche"})
    conn = db.connect()
    prompt = trainer.build_prompt(conn, worker.claim_chat(conn)["id"])
    conn.close()
    return prompt


def test_trainer_sees_appointments_but_not_details(client):
    today = dt.date.today()
    day = (today + dt.timedelta(days=2)).strftime("%Y%m%d")
    body = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:a
DTSTART;TZID=Europe/Berlin:{day}T180000
DTEND;TZID=Europe/Berlin:{day}T200000
SUMMARY:Elternabend
DESCRIPTION:Geheim
END:VEVENT
END:VCALENDAR
""".encode()
    app.state.calendar_transport = transport(body)
    client.post("/api/settings/calendar", json={"urls": SECRET})
    prompt = ask(client)
    assert '"titel": "Elternabend"' in prompt and '"von": "18:00"' in prompt and '"bis": "20:00"' in prompt
    assert "Geheim" not in prompt and "private-0123abcdef" not in prompt


def test_trainer_is_told_when_not_connected(client):
    assert "nicht verbunden, du kennst keine Termine" in ask(client)


def test_failed_refresh_keeps_events_warns_and_tells_trainer(client):
    today = dt.date.today()
    conn = db.connect()
    gcal.save_urls([SECRET])
    gcal.store(conn, [{"start": f"{today}T10:00", "end": f"{today}T11:00", "all_day": 0, "free": 0, "title": "Bleibt", "uid": "x"}])
    conn.commit()
    gcal.refresh(transport(status=404))
    assert conn.execute("SELECT COUNT(*) c FROM calendar_events").fetchone()["c"] == 1
    db.set_setting(conn, "worker_heartbeat", "1000000")
    assert any("lehnt die Kalender-Adresse ab" in a["text"] for a in health.alerts(conn, now=1000060))
    context = gcal.trainer_context(conn, today)
    assert "alter Stand" in context["status"] and context["termine"][0]["titel"] == "Bleibt"
    conn.close()


def test_success_clears_error(client):
    connect(client)
    conn = db.connect()
    db.set_setting(conn, "calendar_error", "alt")
    db.set_setting(conn, "calendar_last_fetch", "1")
    conn.commit()
    gcal.refresh(transport())
    assert not db.get_setting(conn, "calendar_error")
    conn.close()


def test_chat_refreshes_stale_calendar_and_survives_outage(client):
    calls = []
    connect(client)
    conn = db.connect()
    db.set_setting(conn, "calendar_last_fetch", "1")
    conn.commit()
    conn.close()
    app.state.calendar_transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500))
    assert client.post("/api/chat", json={"message": "Hallo"}).status_code == 202
    assert len(calls) == 1
    conn = db.connect()
    worker.process_chat(conn, worker.claim_chat(conn), runner=lambda _: "ok")
    conn.close()
    assert client.post("/api/chat", json={"message": "Nochmal"}).status_code == 202
    assert len(calls) == 2


def test_fresh_calendar_is_not_fetched_again(client):
    calls = []
    connect(client)
    app.state.calendar_transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, content=ICS))
    client.post("/api/chat", json={"message": "Hallo"})
    assert calls == []


def test_disconnect_removes_everything(client):
    connect(client)
    assert client.delete("/api/settings/calendar").status_code == 200
    assert gcal.read_urls() == [] and client.get("/api/settings").json()["calendar_count"] == 0
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM calendar_events").fetchone()["c"] == 0
    conn.close()
    assert "nicht verbunden" in ask(client)


def test_no_network_when_not_connected(anon):
    gcal.refresh(httpx.MockTransport(lambda r: pytest.fail("kein Abruf erwartet")))



def event(start, end, title, uid=None, all_day=0, free=0):
    return {"start": start, "end": end, "all_day": all_day, "free": free, "title": title, "uid": uid or f"{title}|{start}"}


def day(offset):
    return (dt.date.today() + dt.timedelta(days=offset)).isoformat()


def stored(*events):
    conn = db.connect()
    result = gcal.store(conn, list(events))
    conn.commit()
    conn.close()
    return result


def month(client, start=-20, end=20):
    return client.get(f"/api/calendar?start={day(start)}&end={day(end)}").json()


@pytest.mark.parametrize("title,sport", [
    ("Schwimmen 45 Technik", "swim"), ("Schwimmen – Intervalle", "swim"), ("Schwimmen Technik (statt Lauf)", "swim"),
    ("Rad 3x8 Sweetspot 70′", "bike"), ("Kickr: 2×8 Sweetspot (Entlastung)", "bike"), ("FTP-Test 20 Min (Kickr)", "bike"),
    ("Rad Z2 60' (Kickr)", "bike"), ("Rad lang 2–2,5h + Koppellauf 15′", "brick"),
    ("Lange Radausfahrt + Koppellauf", "brick"), ("Rad lang Z2 + Koppel", "brick"), ("Z2-Lauf 75 min", "run"), ("Rad 75–90 Z2 + Koppellauf 15", "brick"),
    ("Koppellauf 15′", "run"), ("Laufband Zone 2 45′", "run"), ("Lauf lang 60′ locker", "run"), ("Joggen", "run"), ("Lauf Z2 30–40′", "run"),
    ("Kraft Oberkörper Push 40′", "strength"), ("Gym: Beine (schwer)", "strength"), ("Oberkörper B (Volumen)", "strength"), ("Beine", "strength"),
    ("Tennis", "other"), ("Paddel", "other"), ("Buchung - TC Blau-Weiß Musterstadt e.V.", "other"),
])
def test_real_training_titles_are_recognised(title, sport):
    assert classify_title(title) == sport


@pytest.mark.parametrize("title", [
    "Vorlesung Programmiersprachen", "Algorithmen & Datenstrukturen", "Rechnungswesen & Controlling",
    "Prozessmanagement und -automatisierung", "Projektmanagement", "Friseur", "Weinfest", "Seminar", "Team Offsite",
    "Seminar Musterstadt. M01-0101-0105", "Ironman Anmeldung", "Meeting mit Kunde", "Pflanzen gießen", "Geburtstagsparty",
    "Wirtschafts- und IT-Recht Hausarbeit", "Verlaufskontrolle", "Fahrradladen abholen", "Radio Interview", "VPS auf SSH umstellen", "Firmen-Training",
])
def test_other_titles_are_not_trainings(title):
    assert classify_title(title) is None


def test_user_terms_override():
    assert classify_title("Bouldern", extra=["bouldern"]) == "other"
    assert classify_title("Rad Reparatur", ignore=["reparatur"]) is None


def test_trainings_become_plan_entries_and_others_stay_for_trainer_only(client):
    result = stored(event(f"{day(1)}T16:00", f"{day(1)}T16:45", "Schwimmen Technik"),
                    event(f"{day(1)}T08:30", f"{day(1)}T12:00", "Algorithmen & Datenstrukturen"))
    assert result == {"trainings": 1, "others": 1}
    data = month(client)
    assert "appointments" not in data
    plan = data["planned"][0]
    assert (plan["sport"], plan["title"], plan["duration_min"], plan["status"]) == ("swim", "Schwimmen Technik", 45, "open")
    assert "16:00 bis 16:45" in plan["notes"]
    assert "appointments" not in client.get("/api/today").json()
    prompt = ask(client)
    assert '"titel": "Algorithmen & Datenstrukturen"' in prompt and "Schwimmen Technik" in prompt


def test_past_training_with_garmin_is_merged(client):
    stored(event(f"{day(-2)}T17:00", f"{day(-2)}T18:10", "Rad 3x8 Sweetspot 70′"))
    conn = db.connect()
    conn.execute("INSERT INTO activities(id,date,start_local,sport,duration_s,raw) VALUES(1,?,?,'bike',4100,'{}')", (day(-2), f"{day(-2)} 17:00:00"))
    conn.commit()
    conn.close()
    plan = month(client)["planned"][0]
    assert plan["status"] == "done" and plan["activity_ids"] == [1]
    assert not [r for r in client.get("/api/activities").json() if r.get("calendar_only")]


def test_past_training_without_garmin_counts_as_trained(client):
    stored(event(f"{day(-1)}T17:00", f"{day(-1)}T18:00", "Gym: Beine"))
    assert month(client)["planned"][0]["status"] == "calendar"
    recent = client.get("/api/activities").json()[0]
    assert recent["calendar_only"] and recent["name"] == "Gym: Beine" and recent["duration_s"] == 3600
    assert client.get("/api/trends?weeks=4").json()["weeks"][-1 if dt.date.fromisoformat(day(-1)).isocalendar()[1] == dt.date.today().isocalendar()[1] else -2]["strength"] == 1.0
    conn = db.connect()
    prompt = coach.build_week_prompt(conn, (dt.date.today() - dt.timedelta(days=dt.date.today().weekday())).isoformat(), dt.date.today())
    conn.close()
    assert '"einheiten": 1' in prompt or dt.date.today().weekday() == 0


def test_this_week_counts_calendar_training(client):
    if dt.date.today().weekday() == 0:
        pytest.skip("Montag: gestern liegt in der Vorwoche")
    stored(event(f"{day(-1)}T17:00", f"{day(-1)}T17:30", "Lauf locker"))
    assert client.get("/api/overview").json()["sports"]["run"]["done_min"] == 30


def test_changes_in_google_arrive_and_deleted_ones_disappear(client):
    stored(event(f"{day(1)}T10:00", f"{day(1)}T11:00", "Schwimmen", uid="a"), event(f"{day(2)}T10:00", f"{day(2)}T11:00", "Rad", uid="b"))
    stored(event(f"{day(1)}T10:00", f"{day(1)}T10:30", "Lauf locker", uid="a"))
    plans = month(client)["planned"]
    assert [(p["title"], p["sport"], p["duration_min"]) for p in plans] == [("Lauf locker", "run", 30)]


def test_no_duplicate_rows_on_repeated_sync(client):
    item = event(f"{day(1)}T10:00", f"{day(1)}T11:00", "Rad", uid="a")
    stored(item)
    stored(item)
    assert len(month(client)["planned"]) == 1


def test_old_entries_outside_window_survive(client):
    conn = db.connect()
    conn.execute("INSERT INTO planned(date,sport,title,google_uid) VALUES(?,'run','Alt','alt')", (day(-400),))
    conn.commit()
    conn.close()
    stored()
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM planned").fetchone()["c"] == 1
    conn.close()


def test_own_plan_is_not_duplicated_or_overwritten(client):
    client.post("/api/planned", json={"date": day(1), "sport": "swim", "title": "Mein Eintrag"})
    assert stored(event(f"{day(1)}T10:00", f"{day(1)}T11:00", "Schwimmen", uid="a"))["trainings"] == 0
    assert [p["title"] for p in month(client)["planned"]] == ["Mein Eintrag"]


def test_google_entries_are_read_only_and_can_be_hidden_for_good(client):
    item = event(f"{day(-1)}T10:00", f"{day(-1)}T11:00", "Rad", uid="a")
    stored(item)
    plan = month(client)["planned"][0]["id"]
    assert client.put(f"/api/planned/{plan}", json={"date": day(1), "sport": "run"}).status_code == 403
    assert client.post(f"/api/planned/{plan}/move", json={"date": day(1)}).status_code == 403
    assert client.delete(f"/api/planned/{plan}").status_code == 200
    assert month(client)["planned"] == [] and not client.get("/api/activities").json()
    stored(item)
    assert month(client)["planned"] == []
    assert client.get("/api/settings").json()["calendar_hidden"] == 1
    assert client.post("/api/settings/calendar-unhide").status_code == 200
    assert len(month(client)["planned"]) == 1


def test_proposal_replace_leaves_google_entries(client):
    stored(event(f"{day(2)}T10:00", f"{day(2)}T11:00", "Rad", uid="a"))
    conn = db.connect()
    proposal = [{"date": day(1), "sport": "run", "title": "x", "duration_min": 30, "distance_km": None, "intensity": "", "notes": ""},
                {"date": day(3), "sport": "run", "title": "y", "duration_min": 30, "distance_km": None, "intensity": "", "notes": ""}]
    assert trainer.conflicts(conn, proposal) == 0
    result = trainer.apply_proposal(conn, proposal, True)
    conn.commit()
    conn.close()
    assert result["replaced"] == 0
    assert sorted(p["title"] for p in month(client)["planned"]) == ["Rad", "x", "y"]


def test_terms_setting_reclassifies(client):
    item = event(f"{day(1)}T10:00", f"{day(1)}T11:00", "Bouldern", uid="a")
    wrong = event(f"{day(2)}T10:00", f"{day(2)}T11:00", "Rad Reparatur", uid="b")
    assert stored(item, wrong)["trainings"] == 1
    conn = db.connect()
    db.set_setting(conn, "calendar_training_terms", "bouldern")
    db.set_setting(conn, "calendar_ignore_terms", "reparatur")
    conn.commit()
    conn.close()
    stored(item, wrong)
    assert [(p["title"], p["sport"]) for p in month(client)["planned"]] == [("Bouldern", "other")]


def test_past_private_events_are_not_stored_but_running_ones_stay(client):
    stored(event(f"{day(-3)}T10:00", f"{day(-3)}T11:00", "Vergangen"),
           event(day(-1), day(1), "Dienstreise", all_day=1),
           event(f"{day(1)}T10:00", f"{day(1)}T11:00", "Künftig"))
    conn = db.connect()
    titles = sorted(row["title"] for row in conn.execute("SELECT title FROM calendar_events"))
    conn.close()
    assert titles == ["Dienstreise", "Künftig"]


def test_disconnect_removes_google_entries_but_keeps_own(client):
    client.post("/api/planned", json={"date": day(5), "sport": "swim", "title": "Mein Eintrag"})
    stored(event(f"{day(1)}T10:00", f"{day(1)}T11:00", "Rad", uid="a"))
    client.delete("/api/settings/calendar")
    assert [p["title"] for p in month(client)["planned"]] == ["Mein Eintrag"]


def test_history_is_loaded_from_ical():
    today = dt.date.today()
    data = (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//DE\r\nBEGIN:VEVENT\r\nUID:a\r\n"
            f"DTSTAMP:20260101T000000Z\r\nSUMMARY:Rad\r\nDTSTART;VALUE=DATE:{today - dt.timedelta(days=100):%Y%m%d}\r\n"
            f"END:VEVENT\r\nEND:VCALENDAR\r\n").encode()
    found = gcal.parse_events(data, today - dt.timedelta(days=gcal.PAST_DAYS), gcal.PAST_DAYS + gcal.HORIZON_DAYS)
    assert [e["title"] for e in found] == ["Rad"]
