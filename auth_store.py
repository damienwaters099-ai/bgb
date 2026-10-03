"""
Two-tier user login — named accounts with a role, instead of one shared
password for everyone.
=========================================================================
Replaces the single shared APP_USERNAME/APP_PASSWORD with a small list of
named users, each with a role:

  - "admin" — full access, including the Brush stock admin screen and the
    user-management screen (add/remove people, change roles) at /admin/users.
  - "user"  — everything else (Opportunities, Brush, TCO) — no admin screens.

WHERE USERS ARE STORED
------------------------
A JSON file at APP_DATA_DIR/users.json (same folder persistence.py already
uses for the opportunity/enquiry logs). Passwords are never stored in plain
text — only a salted PBKDF2 hash, so the file is safe even if someone reads
it off disk.

FIRST RUN / BACKWARDS COMPATIBILITY
--------------------------------------
If users.json doesn't exist yet, it's seeded from the old env vars
(APP_USERNAME/APP_PASSWORD as a "user" account, ADMIN_USERNAME/ADMIN_PASSWORD
as an "admin" account) so nothing breaks on first deploy after this change.
From then on, manage people through /admin/users instead of env vars.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Literal

Role = Literal["user", "admin"]

DATA_DIR = Path(os.getenv("APP_DATA_DIR", Path(__file__).resolve().parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_PATH = DATA_DIR / "users.json"

_PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS).hex()


def _load_users() -> list[dict]:
    if not USERS_PATH.exists():
        _seed_from_env()
    with open(USERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_users(users: list[dict]) -> None:
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)


def _seed_from_env() -> None:
    legacy_user = os.getenv("APP_USERNAME", "bgb")
    legacy_pass = os.getenv("APP_PASSWORD", "bgb2026")
    legacy_admin_user = os.getenv("ADMIN_USERNAME", "admin")
    legacy_admin_pass = os.getenv("ADMIN_PASSWORD", "bgbadmin2026")
    seeded = [
        _make_user_record(legacy_user, legacy_pass, "user"),
        _make_user_record(legacy_admin_user, legacy_admin_pass, "admin"),
    ]
    _save_users(seeded)


def _make_user_record(username: str, password: str, role: Role) -> dict:
    salt = secrets.token_hex(16)
    return {"username": username, "salt": salt, "password_hash": _hash_password(password, salt), "role": role}


def verify_user(username: str, password: str) -> Role | None:
    """Returns the user's role if the username/password match, else None."""
    for record in _load_users():
        if secrets.compare_digest(record["username"], username):
            candidate_hash = _hash_password(password, record["salt"])
            if hmac.compare_digest(candidate_hash, record["password_hash"]):
                return record["role"]
            return None
    return None


def list_users() -> list[dict]:
    """Usernames and roles only — never returns password hashes."""
    return [{"username": r["username"], "role": r["role"]} for r in _load_users()]


def add_user(username: str, password: str, role: Role) -> None:
    users = _load_users()
    users = [u for u in users if u["username"] != username]  # replace if it already exists
    users.append(_make_user_record(username, password, role))
    _save_users(users)


def delete_user(username: str) -> bool:
    users = _load_users()
    remaining = [u for u in users if u["username"] != username]
    if len(remaining) == len(users):
        return False
    _save_users(remaining)
    return True
