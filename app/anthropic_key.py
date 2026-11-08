"""Eigener Anthropic-API-Key eines Benutzers. Liegt in seinem Datenordner, nur für den Dienst lesbar,
und wird nicht mitgesichert."""
import os
import re

from . import db

PATTERN = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,300}$")


def _path():
    return db.data_dir() / "anthropic.key"


def read_key() -> str | None:
    try:
        return _path().read_text().strip() or None
    except FileNotFoundError:
        return None


def save_key(key: str) -> None:
    path = _path()
    tmp = path.with_name(".anthropic.key.tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as out:
        out.write(key)
    os.replace(tmp, path)


def delete_key() -> None:
    _path().unlink(missing_ok=True)


def masked(key: str | None) -> str:
    return f"{key[:10]}…{key[-4:]}" if key else ""
