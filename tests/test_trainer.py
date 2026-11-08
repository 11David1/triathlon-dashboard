import datetime as dt
import json

from app import coach, db, trainer, worker

TODAY = dt.date(2026, 11, 7)


def plan_tag(*items):
    return "<trainingsplan>" + json.dumps(list(items)) + "</trainingsplan>"


ITEM = {"date": "2026-11-09", "sport": "run", "title": "Locker", "duration_min": 50, "intensity": "Zone 2"}


def test_reply_without_plan():
    assert trainer.parse_reply("Alles gut.", TODAY) == ("Alles gut.", None, None)


def test_reply_with_plan_is_split_and_sorted():
    later = {**ITEM, "date": "2026-11-12", "sport": "swim"}
    text, proposal, note = trainer.parse_reply("Mein Rat.\n" + plan_tag(later, ITEM), TODAY)
    assert text == "Mein Rat." and note is None
    assert [item["date"] for item in proposal] == ["2026-11-09", "2026-11-12"]
    assert proposal[0]["duration_min"] == 50 and proposal[0]["distance_km"] is None


def test_code_fence_in_tag_is_tolerated():
    _, proposal, _ = trainer.parse_reply("x<trainingsplan>```json\n" + json.dumps([ITEM]) + "\n```</trainingsplan>", TODAY)
    assert proposal and proposal[0]["sport"] == "run"


def test_invalid_entries_are_dropped_with_note():
    rows = [ITEM, {**ITEM, "date": "2026-11-01"}, {**ITEM, "sport": "golf"}, {**ITEM, "date": "morgen"},
            {**ITEM, "date": "2027-06-01"}, "kaputt", {**ITEM, "duration_min": 99999}]
    text, proposal, note = trainer.parse_reply("Rat" + plan_tag(*rows), TODAY)
    assert len(proposal) == 2 and "5 Einheiten" in note
    assert proposal[1]["duration_min"] is None


def test_broken_or_cut_off_plan_never_shows_raw_tag():
    text, proposal, note = trainer.parse_reply("Rat <trainingsplan>[{kaputt", TODAY)
    assert text == "Rat" and proposal is None and note
    text, proposal, note = trainer.parse_reply("Rat <trainingsplan>{\"a\":1}</trainingsplan>", TODAY)
    assert text == "Rat" and proposal is None and note
    text, proposal, note = trainer.parse_reply("Rat <trainingsplan>[]</trainingsplan>", TODAY)
    assert text == "Rat" and proposal is None and note


def test_chat_flow_does_not_touch_calendar_until_applied(client):
    today = dt.date.today()
    day = (today + dt.timedelta(days=2)).isoformat()
    assert client.post("/api/chat", json={"message": "Plane meine Woche"}).status_code == 202
    assert client.post("/api/chat", json={"message": "Noch was"}).status_code == 409
    conn = db.connect()
    row = worker.claim_chat(conn)
    assert row["role"] == "assistant" and row["status"] == "pending"
    seen = []

    def runner(prompt):
        seen.append(prompt)
        return "Mach das so.\n" + plan_tag({**ITEM, "date": day})

    worker.process_chat(conn, row, runner=runner)
    assert "Plane meine Woche" in seen[0] and "<daten>" in seen[0]
    conn.close()
    messages = client.get("/api/chat").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Mach das so." and messages[1]["proposal_status"] == "open"
    assert messages[1]["conflicts"] == 0
    assert client.get(f"/api/calendar?start={day}&end={day}").json()["planned"] == []
    result = client.post(f"/api/chat/{messages[1]['id']}/apply", json={"replace": False})
    assert result.status_code == 200 and result.json()["added"] == 1
    assert client.get(f"/api/calendar?start={day}&end={day}").json()["planned"][0]["title"] == "Locker"
    assert client.post(f"/api/chat/{messages[1]['id']}/apply", json={"replace": False}).status_code == 409
    assert len(client.get(f"/api/calendar?start={day}&end={day}").json()["planned"]) == 1


def proposal_message(client, runner_items):
    client.post("/api/chat", json={"message": "Plan"})
    conn = db.connect()
    worker.process_chat(conn, worker.claim_chat(conn), runner=lambda _: "ok" + plan_tag(*runner_items))
    conn.close()
    return client.get("/api/chat").json()[-1]


def test_replace_removes_only_open_entries_in_range(client):
    today = dt.date.today()
    first = (today + dt.timedelta(days=1)).isoformat()
    last = (today + dt.timedelta(days=3)).isoformat()
    outside = (today + dt.timedelta(days=9)).isoformat()
    for date, title in ((first, "A"), (last, "B"), (outside, "Draussen")):
        client.post("/api/planned", json={"date": date, "sport": "bike", "title": title})
    message = proposal_message(client, [{**ITEM, "date": first}, {**ITEM, "date": last, "title": "Neu"}])
    assert message["conflicts"] == 2
    result = client.post(f"/api/chat/{message['id']}/apply", json={"replace": True}).json()
    assert result == {"added": 2, "replaced": 2, "first_date": first}
    titles = [p["title"] for p in client.get(f"/api/calendar?start={today}&end={outside}").json()["planned"]]
    assert sorted(titles) == ["Draussen", "Locker", "Neu"]


def test_append_keeps_existing_entries(client):
    day = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    client.post("/api/planned", json={"date": day, "sport": "bike", "title": "Alt"})
    message = proposal_message(client, [{**ITEM, "date": day}])
    client.post(f"/api/chat/{message['id']}/apply", json={"replace": False})
    assert len(client.get(f"/api/calendar?start={day}&end={day}").json()["planned"]) == 2


def test_replace_keeps_todays_completed_plan(client):
    today = dt.date.today()
    client.post("/api/planned", json={"date": today.isoformat(), "sport": "run", "title": "Erledigt"})
    client.post("/api/activities", json={"date": today.isoformat(), "time": "00:00", "sport": "run", "duration_min": 30})
    message = proposal_message(client, [{**ITEM, "date": today.isoformat(), "title": "Neu"}])
    assert message["conflicts"] == 0
    client.post(f"/api/chat/{message['id']}/apply", json={"replace": True})
    titles = [p["title"] for p in client.get(f"/api/calendar?start={today}&end={today}").json()["planned"]]
    assert "Erledigt" in titles


def test_stale_proposal_skips_past_days():
    conn = db.connect()
    db.init()
    result = trainer.apply_proposal(conn, [{**ITEM, "title": "x", "distance_km": None, "notes": ""}], False, today=dt.date(2026, 11, 20))
    assert result == {"added": 0, "replaced": 0}
    conn.close()


def test_discard_and_clear(client):
    day = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    message = proposal_message(client, [{**ITEM, "date": day}])
    assert client.post(f"/api/chat/{message['id']}/discard").status_code == 200
    assert client.post(f"/api/chat/{message['id']}/apply", json={"replace": False}).status_code == 409
    assert client.get("/api/chat").json()[-1]["proposal_status"] == "discarded"
    assert client.delete("/api/chat").status_code == 200
    assert client.get("/api/chat").json() == []


def test_failure_can_be_retried(client):
    client.post("/api/chat", json={"message": "Hallo"})
    conn = db.connect()

    def boom(_):
        raise RuntimeError("Anmeldung abgelaufen")

    row = worker.claim_chat(conn)
    worker.process_chat(conn, row, runner=boom)
    failed = client.get("/api/chat").json()[-1]
    assert failed["status"] == "failed" and "Anmeldung" in failed["error"]
    assert client.post("/api/chat", json={"message": "Neu"}).status_code == 202
    conn.execute("DELETE FROM chat_messages WHERE status='pending'")
    conn.commit()
    assert client.post(f"/api/chat/{failed['id']}/retry").status_code == 202
    assert client.post("/api/chat/99999/retry").status_code == 404
    row = worker.claim_chat(conn)
    worker.process_chat(conn, row, runner=lambda _: "Klappt")
    assert [m["content"] for m in client.get("/api/chat").json() if m["role"] == "assistant"][-1] == "Klappt"
    conn.close()


def test_message_validation(client):
    assert client.post("/api/chat", json={"message": "   "}).status_code == 400
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert client.post("/api/chat", json={"message": "x" * 4001}).status_code == 422


def test_prompt_has_context_history_and_keeps_privacy(client):
    conn = db.connect()
    raw = {"activityName": "Musterstadt Laufen", "locationName": "Musterstadt", "startLatitude": 51.43, "ownerFullName": "Geheim"}
    day = dt.date.today().isoformat()
    conn.execute("INSERT INTO activities(id,date,start_local,sport,name,duration_s,raw,note) VALUES(7,?,?,'run',?,3600,?,'Wade zwickt')",
                 (day, f"{day} 07:00:00", "Musterstadt Laufen", json.dumps(raw)))
    conn.execute("INSERT INTO wellness(date,hrv,resting_hr) VALUES(?,58,46)", (day,))
    conn.commit()
    client.put("/api/settings", json={"race_name": "Rennen X", "race_date": "2027-08-29", "athlete_notes": "FTP 250"})
    client.post("/api/chat", json={"message": "Erste Frage"})
    worker.process_chat(conn, worker.claim_chat(conn), runner=lambda _: "Erste Antwort")
    client.post("/api/chat", json={"message": "Zweite Frage"})
    row = worker.claim_chat(conn)
    prompt = trainer.build_prompt(conn, row["id"])
    for needle in ("Erste Frage", "Erste Antwort", "Zweite Frage", "Wade zwickt", "FTP 250", "Rennen X", "naechste_woche",
                   '"hrv": 58', '"minuten": 60', "eigene_notiz"):
        assert needle in prompt, needle
    for secret in ("Musterstadt", "51.43", "Geheim"):
        assert secret not in prompt
    assert prompt.rindex("Zweite Frage") > prompt.rindex("</verlauf>")
    conn.close()


def test_history_includes_proposal_for_follow_ups(client):
    day = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    proposal_message(client, [{**ITEM, "date": day}])
    client.post("/api/chat", json={"message": "Mach Dienstag kürzer"})
    conn = db.connect()
    prompt = trainer.build_prompt(conn, worker.claim_chat(conn)["id"])
    conn.close()
    assert "Planvorschlag, noch offen" in prompt and "Locker" in prompt


def test_trainer_runs_without_tools():
    command = worker.claude_command(trainer.SYSTEM)
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--system-prompt") + 1] == trainer.SYSTEM


def minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
    objects = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
               b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
               b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
               b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % start
    return bytes(out)


def ask(client):
    client.post("/api/chat", json={"message": "Frage"})
    conn = db.connect()
    prompt = trainer.build_prompt(conn, worker.claim_chat(conn)["id"])
    conn.close()
    return prompt


def test_instructions_and_documents_reach_the_trainer(client):
    assert client.put("/api/knowledge/instructions", json={"instructions": "Zielzeit unter 5:30"}).status_code == 200
    assert client.post("/api/knowledge", json={"title": "Strecke", "content": "Radstrecke ist flach"}).status_code == 201
    prompt = ask(client)
    assert "<projekt_anweisungen>\nZielzeit unter 5:30" in prompt
    assert '<dokument titel="Strecke">\nRadstrecke ist flach' in prompt
    assert prompt.index("<wissen>") < prompt.index("<daten>")


def test_no_knowledge_means_no_empty_blocks(client):
    prompt = ask(client)
    assert "<wissen>" not in prompt and "<projekt_anweisungen>" not in prompt


def test_text_and_pdf_upload(client):
    upload = client.post("/api/knowledge/file?name=plan.md", content="# Plan\nGrüße".encode())
    assert upload.status_code == 201
    pdf = client.post("/api/knowledge/file?name=Strecke.pdf", content=minimal_pdf("Wechselzone am Regattabahn"))
    assert pdf.status_code == 201
    docs = client.get("/api/knowledge").json()["documents"]
    assert [d["title"] for d in docs] == ["plan.md", "Strecke.pdf"]
    prompt = ask(client)
    assert "Grüße" in prompt and "Wechselzone" in prompt
    assert client.delete(f"/api/knowledge/{docs[0]['id']}").status_code == 200
    assert len(client.get("/api/knowledge").json()["documents"]) == 1


def test_bad_uploads_are_rejected(client):
    assert client.post("/api/knowledge/file?name=x.pdf", content=b"kein pdf").status_code == 400
    assert client.post("/api/knowledge/file?name=x.docx", content=b"abc").status_code == 400
    assert client.post("/api/knowledge/file?name=x.txt", content=b"").status_code == 400
    assert client.post("/api/knowledge/file?name=x.txt", content=b"   ").status_code == 400
    assert client.post("/api/knowledge/file?name=x.txt", content=b"x" * (10 * 1024 * 1024 + 1)).status_code == 413
    assert client.get("/api/knowledge").json()["documents"] == []


def test_upload_name_cannot_escape_and_latin1_works(client):
    client.post("/api/knowledge/file?name=../../etc/passwd.txt", content="Größe".encode("latin-1"))
    doc = client.get("/api/knowledge").json()["documents"][0]
    assert doc["title"] == "passwd.txt"
    assert "Größe" in ask(client)


def test_knowledge_is_capped_in_prompt(client):
    for index in range(2):
        client.post("/api/knowledge/file?name=d%d.txt" % index, content=(("ab" if index else "cd") * 50000).encode())
    conn = db.connect()
    block = trainer.knowledge_block(conn)
    conn.close()
    assert block.count("cd") + block.count("ab") <= trainer.MAX_KNOWLEDGE_CHARS // 2 + 10


def test_long_document_is_truncated_and_reported(client):
    result = client.post("/api/knowledge/file?name=lang.txt", content=b"x" * 150000).json()
    assert result["truncated"] is True
    assert client.get("/api/knowledge").json()["documents"][0]["chars"] == 100000


def test_today_view(client):
    today = dt.date.today()
    conn = db.connect()
    conn.execute("INSERT INTO wellness(date,hrv,resting_hr) VALUES(?,60,44)", (today.isoformat(),))
    for back in range(1, 7):
        conn.execute("INSERT INTO wellness(date,hrv) VALUES(?,50)", ((today - dt.timedelta(days=back)).isoformat(),))
    conn.commit()
    conn.close()
    client.post("/api/planned", json={"date": today.isoformat(), "sport": "run", "title": "Heute"})
    client.post("/api/planned", json={"date": (today + dt.timedelta(days=1)).isoformat(), "sport": "rest"})
    client.post("/api/planned", json={"date": (today + dt.timedelta(days=2)).isoformat(), "sport": "bike", "title": "Danach"})
    view = client.get("/api/today").json()
    assert view["planned"][0]["title"] == "Heute" and view["planned"][0]["status"] == "open"
    assert view["next"]["title"] == "Danach"
    assert view["recovery"]["hrv"] == {"value": 60, "date": today.isoformat(), "average": 50.0}
    assert view["recovery"]["resting_hr"]["value"] == 44 and view["recovery"]["resting_hr"]["average"] is None
    assert view["recovery"]["sleep_s"]["value"] is None


def test_old_recovery_value_is_not_shown_as_current(client):
    old = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    conn = db.connect()
    conn.execute("INSERT INTO wellness(date,hrv) VALUES(?,60)", (old,))
    conn.commit()
    conn.close()
    assert client.get("/api/today").json()["recovery"]["hrv"]["value"] is None
