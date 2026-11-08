"""Ben nutzt nur seinen eigenen API-Key, Anna weiter das Abo des Servers."""
import os
import subprocess

import pytest
from conftest import PASSWORD

from app import anthropic_key, db, users, worker

KEY = "sk-ant-api03-" + "x" * 40


def _login_ben(client):
    code = users.add("ben", "Ben")
    response = client.post("/api/login", json={"username": "ben", "password": code, "new_password": "bens-eigenes-passwort"})
    assert response.status_code == 200
    return users.get("ben")


def test_anna_stays_on_subscription(client):
    anna = users.get("anna")
    assert not anna.own_key
    assert client.get("/api/settings").json()["claude_own_key"] is False
    assert client.post("/api/settings/claude-key", json={"key": KEY}).status_code == 400
    assert worker.runners_for(anna) == (worker.run_claude, worker._ask_trainer)


def test_new_users_default_to_own_key(anon):
    assert users.get("ben") is None
    users.add("ben", "Ben")
    assert users.get("ben").own_key


def test_ben_without_key_gets_no_claude(client):
    ben = _login_ben(client)
    assert client.post("/api/chat", json={"message": "Hallo"}).status_code == 400
    alerts = client.get("/api/overview").json()["alerts"]
    assert any("API-Key" in a["text"] for a in alerts)
    with db.use(ben.dir), pytest.raises(worker.NoKey):
        worker.runners_for(ben)


def test_ben_saves_key_privately(client):
    ben = _login_ben(client)
    assert client.post("/api/settings/claude-key", json={"key": "kein-key-aber-lang-genug"}).status_code == 400
    assert client.post("/api/settings/claude-key", json={"key": KEY}).status_code == 200
    settings = client.get("/api/settings").json()
    assert KEY not in str(settings) and settings["claude_key"].startswith("sk-ant-api")
    path = ben.dir / "anthropic.key"
    assert path.read_text() == KEY and (path.stat().st_mode & 0o777) == 0o600
    assert client.post("/api/chat", json={"message": "Hallo"}).status_code == 202
    assert not any("API-Key" in a["text"] for a in client.get("/api/overview").json()["alerts"])
    # Anna sieht Bens Key nicht und hat keinen eigenen.
    assert client.post("/api/login", json={"username": "anna", "password": PASSWORD}).status_code == 200
    assert not (db.ROOT_DIR / "anthropic.key").exists()
    assert client.delete("/api/settings/claude-key").status_code == 200
    assert path.exists()


def test_key_call_never_sees_root_login(client, monkeypatch):
    ben = _login_ben(client)
    with db.use(ben.dir):
        anthropic_key.save_key(KEY)
        analyse, chat = worker.runners_for(ben)
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="Antwort", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "abo-token")
    assert chat("Frage") == "Antwort"
    env = seen["env"]
    assert env["ANTHROPIC_API_KEY"] == KEY
    assert env["HOME"] == seen["cwd"] and env["HOME"] != os.environ.get("HOME")
    assert not any("OAUTH" in name or name.startswith("CLAUDE") for name in env)
    assert seen["command"][seen["command"].index("--model") + 1] == worker.API_MODEL


def test_subscription_call_unchanged(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    worker.run_claude("Frage")
    assert seen["env"] is None and "--model" not in seen["command"]


def test_operator_can_grant_and_revoke_subscription(anon):
    users.add("ben", "Ben")
    users.set_claude("ben", users.CLAUDE_ABO)
    assert not users.get("ben").own_key
    users.set_claude("ben", users.CLAUDE_API_KEY)
    assert users.get("ben").own_key
    with pytest.raises(ValueError):
        users.set_claude("ben", "gratis")
    with pytest.raises(ValueError):
        users.set_claude("niemand", users.CLAUDE_ABO)
