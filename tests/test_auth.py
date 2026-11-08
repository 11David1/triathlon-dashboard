from conftest import PASSWORD

from app import auth, db
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


def test_login_fails_closed_without_password(anon):
    conn = db.connect()
    conn.execute("DELETE FROM settings")
    conn.commit()
    conn.close()
    assert anon.post("/api/login", json={"password": ""}).status_code == 401
    assert anon.post("/api/login", json={"password": "egal"}).status_code == 401


def test_wrong_password_rejected_and_throttled(anon):
    for _ in range(5):
        assert anon.post("/api/login", json={"password": "falsch"}).status_code == 401
    assert anon.post("/api/login", json={"password": PASSWORD}).status_code == 429


def test_cookie_flags(anon):
    response = anon.post("/api/login", json={"password": PASSWORD})
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
    conn = db.connect()
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
