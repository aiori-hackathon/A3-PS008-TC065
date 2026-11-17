"""Users, roles and sessions.

Roles (each includes the ones before it):
  operator  submit changes and plain-language requests, see everything
  approver  approve or reject changes (never their own: separation of duties is enforced in the engine)
  admin     create, deploy, stop and delete labs; manage users; accept or revert drift

Passwords are stored as scrypt hashes. Sessions are random tokens in an HttpOnly cookie, kept in memory
(ponytail: a server restart signs everyone out; store sessions in SQLite if that matters).
On first start with no users, an `admin` account is created and its one-time password is written to
data/initial-admin-password (readable only by you).
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing

ROLES = ["operator", "approver", "admin"]
SESSION_HOURS = 8
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_sessions = {}  # token -> (name, role, expires)


class AuthError(ValueError):
    pass


def _db(path):
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, role TEXT, salt BLOB, hash BLOB, created REAL)")
    return db


def _hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)


def add_user(path, name, role, password):
    if not NAME.match(name or ""):
        raise AuthError("user name: letters, digits, . _ - (up to 32)")
    if role not in ROLES:
        raise AuthError(f"role must be one of {', '.join(ROLES)}")
    if len(password or "") < 10:
        raise AuthError("password must be at least 10 characters")
    salt = os.urandom(16)
    with closing(_db(path)) as db:
        db.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?)", (name, role, salt, _hash(password, salt), time.time()))
        db.commit()


def users(path):
    with closing(_db(path)) as db:
        return [{"name": n, "role": r} for n, r in db.execute("SELECT name, role FROM users ORDER BY name")]


def bootstrap(path, secret_file):
    """Create the first admin if there are no users. Returns the password once, else None."""
    if users(path):
        return None
    password = secrets.token_urlsafe(12)
    add_user(path, "admin", "admin", password)
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(f"user: admin\npassword: {password}\n")
    os.chmod(secret_file, 0o600)
    return password


def login(path, name, password):
    with closing(_db(path)) as db:
        row = db.execute("SELECT role, salt, hash FROM users WHERE name = ?", (name,)).fetchone()
    # compare against a dummy hash when the user does not exist, so timing does not reveal valid names
    role, salt, stored = row or ("", b"0" * 16, b"")
    ok = hmac.compare_digest(_hash(password or "", salt), stored) and row is not None
    if not ok:
        raise AuthError("wrong user name or password")
    token = secrets.token_urlsafe(32)
    _sessions[token] = (name, role, time.time() + SESSION_HOURS * 3600)
    return token, {"name": name, "role": role}


def session(token):
    s = _sessions.get(token or "")
    if not s or s[2] < time.time():
        _sessions.pop(token or "", None)
        return None
    return {"name": s[0], "role": s[1]}


def logout(token):
    _sessions.pop(token or "", None)


def allowed(user, role):
    return user is not None and ROLES.index(user["role"]) >= ROLES.index(role)
