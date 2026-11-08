from conftest import PASSWORD

from app import auth, db, users
from app.main import app

PUBLIC_OK = {"/login", "/api/login", "/static/style.css", "/static/login.js", "/healthz",
             "/api/whoop/callback"}  # durch den einmaligen state geschützt, siehe test_whoop.py


def test_every_route_needs_login(anon):
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or path in PUBLIC_OK or path == "/static":
            continue
        url = path.replace("{plan_id}", "1").replace("{activity_id}", "1")
        for method in getattr(route, "methods", {"GET"}) - {"HEAD"}:
            response = anon.request(method, url, json={}, follow_redirects=False)
            assert response.status_code in (401, 303), f"{method} {path}: {response.status_code}"


def test_private_static_files_need_login(anon):
    for path in ("/static/app.js", "/static/index.html", "/"):
        assert anon.get(path, follow_redirects=False).status_code == 303


def login(client, name, password, new_password=None):
    body = {"username": name, "password": password}
    if new_password is not None:
        body["new_password"] = new_password
    return client.post("/api/login", json=body)


def test_login_fails_closed_without_password(anon):
    conn = users.connect()
    conn.execute("UPDATE users SET password_hash=NULL")
    conn.commit()
    conn.close()
    assert login(anon, "anna", "").status_code == 401
    assert login(anon, "anna", "egal").status_code == 401


def test_unknown_user_rejected(anon):
    assert login(anon, "niemand", PASSWORD).status_code == 401


def test_wrong_password_rejected_and_throttled(anon):
    for _ in range(5):
        assert login(anon, "anna", "falsch").status_code == 401
    assert login(anon, "anna", PASSWORD).status_code == 429


def test_username_case_insensitive(anon):
    assert login(anon, " Anna ", PASSWORD).status_code == 200


def test_cookie_flags(anon):
    response = login(anon, "anna", PASSWORD)
    cookie = response.headers["set-cookie"]
    for flag in ("__Host-tri_session=", "HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag in cookie


def test_logout_invalidates_session(client):
    assert client.get("/api/overview").status_code == 200
    token = client.cookies.get("__Host-tri_session")
    assert client.post("/api/logout").status_code == 200
    client.cookies.set("__Host-tri_session", token)
    assert client.get("/api/overview").status_code == 401


def test_session_token_not_stored_in_clear(client):
    token = client.cookies.get("__Host-tri_session")
    conn = users.connect()
    stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM sessions")]
    conn.close()
    assert stored and token not in stored


def test_foreign_origin_blocked(client):
    response = client.post("/api/planned", headers={"Origin": "https://evil.example"},
                           json={"date": "2026-12-01", "sport": "run"})
    assert response.status_code == 403


def test_security_headers(anon):
    headers = anon.get("/login").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"


def test_password_hash_roundtrip():
    stored = auth.hash_password("geheim-geheim")
    assert auth.verify_password("geheim-geheim", stored)
    assert not auth.verify_password("anders", stored)
    assert not auth.verify_password("x", None)


# --- Weitere Benutzer ------------------------------------------------------

def _ben(anon):
    code = users.add("ben", "Ben")
    return code


def test_new_user_must_set_password_on_first_login(anon):
    code = _ben(anon)
    first = login(anon, "ben", code)
    assert first.status_code == 403 and first.json()["setup"] is True
    assert "set-cookie" not in first.headers
    assert login(anon, "ben", code, "zu-kurz").status_code == 400
    assert login(anon, "ben", code, code).status_code == 400
    done = login(anon, "ben", code, "bens-eigenes-passwort")
    assert done.status_code == 200 and "__Host-tri_session=" in done.headers["set-cookie"]
    assert anon.get("/api/overview").status_code == 200
    # Startcode verfällt, das neue Passwort gilt und liegt nur gehasht vor.
    assert login(anon, "ben", code).status_code == 401
    assert login(anon, "ben", "bens-eigenes-passwort").status_code == 200
    conn = users.connect()
    row = conn.execute("SELECT password_hash, setup_hash FROM users WHERE name='ben'").fetchone()
    conn.close()
    assert row["setup_hash"] is None
    assert "bens-eigenes-passwort" not in row["password_hash"]
    assert auth.verify_password("bens-eigenes-passwort", row["password_hash"])


def test_wrong_start_code_does_not_reveal_setup(anon):
    _ben(anon)
    response = login(anon, "ben", "aaaa-bbbb-cccc-dddd", "bens-eigenes-passwort")
    assert response.status_code == 401
    assert login(anon, "ben", "falsch").status_code == 401


def test_expired_start_code_rejected(anon):
    code = _ben(anon)
    conn = users.connect()
    conn.execute("UPDATE users SET setup_expires=1 WHERE name='ben'")
    conn.commit()
    conn.close()
    assert login(anon, "ben", code, "bens-eigenes-passwort").status_code == 401


def test_users_see_only_their_own_data(client):
    assert client.post("/api/planned", json={"date": "2026-12-01", "sport": "run", "title": "Annas Lauf"}).status_code == 201
    code = _ben(client)
    assert login(client, "ben", code, "bens-eigenes-passwort").status_code == 200
    rows = client.get("/api/calendar", params={"start": "2026-11-30", "end": "2026-12-06"}).json()
    assert "Annas Lauf" not in str(rows)
    assert client.get("/api/settings").json()["athlete_name"] == "Ben"
    assert client.post("/api/planned", json={"date": "2026-12-02", "sport": "bike", "title": "Bens Rad"}).status_code == 201
    assert (db.ROOT_DIR / "users" / "ben" / "triathlon.db").exists()
    assert login(client, "anna", PASSWORD).status_code == 200
    rows = str(client.get("/api/calendar", params={"start": "2026-11-30", "end": "2026-12-06"}).json())
    assert "Annas Lauf" in rows and "Bens Rad" not in rows
    assert client.get("/api/settings").json()["athlete_name"] == "Anna"


def test_reset_logs_out_and_issues_new_code(anon):
    code = _ben(anon)
    assert login(anon, "ben", code, "bens-eigenes-passwort").status_code == 200
    new_code = users.reset("ben")
    assert anon.get("/api/overview").status_code == 401
    assert login(anon, "ben", "bens-eigenes-passwort").status_code == 401
    assert login(anon, "ben", new_code).status_code == 403


def test_bad_usernames_refused(anon):
    for name in ("", "a", "../anna", "Ben", "x" * 40):
        try:
            users.add(name, "X")
        except ValueError:
            continue
        raise AssertionError(name)


def test_migrates_single_password_to_admin(anon, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "ROOT_DIR", tmp_path)
    legacy = db.open_db(tmp_path / "triathlon.db")
    legacy.executescript(db.SCHEMA + "CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);")
    legacy.execute("INSERT INTO settings VALUES('password_hash', ?)", (auth.hash_password("altes-passwort-123"),))
    legacy.execute("INSERT INTO sessions VALUES('abc', 9999999999)")
    legacy.commit()
    legacy.close()
    users.init()
    admin = users.check_login("admin", "altes-passwort-123")
    assert admin and admin.dir == tmp_path.resolve()
    assert not admin.own_key  # Der übernommene Benutzer bleibt beim Abo
    conn = users.connect()
    assert conn.execute("SELECT user FROM sessions WHERE token_hash='abc'").fetchone()["user"] == users.LEGACY_USER == "admin"
    conn.close()
