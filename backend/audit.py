"""SQLite audit log. `changes` holds the current state of each change; `events` is append-only,
one row per stage, and each row stores the hash of the previous row so tampering breaks the chain."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, created_at REAL, status TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, change_id TEXT, ts TEXT, stage TEXT, result TEXT, detail TEXT,
  prev_hash TEXT, hash TEXT);
"""


def _hash(prev, change_id, ts, stage, result, detail):
    return hashlib.sha256("|".join([prev, change_id, ts, stage, result, detail]).encode()).hexdigest()


class Audit:
    def __init__(self, path):
        self.path = str(path)
        self._run(lambda db: db.executescript(SCHEMA))

    def _run(self, fn):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            out = fn(db)
            db.commit()
            return out

    def save(self, c):
        self._run(lambda db: db.execute("INSERT OR REPLACE INTO changes VALUES (?, ?, ?, ?)",
                                        (c["id"], c["created_at"], c["status"], json.dumps(c))))

    def get(self, change_id):
        row = self._run(lambda db: db.execute("SELECT data FROM changes WHERE id = ?", (change_id,)).fetchone())
        return json.loads(row["data"]) if row else None

    def list(self):
        rows = self._run(lambda db: db.execute("SELECT data FROM changes ORDER BY created_at DESC").fetchall())
        return [json.loads(r["data"]) for r in rows]

    def event(self, change_id, stage, result, detail=""):
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        def write(db):
            last = db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else "0" * 64
            db.execute("INSERT INTO events (change_id, ts, stage, result, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                       (change_id, ts, stage, result, detail, prev, _hash(prev, change_id, ts, stage, result, detail)))
        self._run(write)

    def events(self, change_id=None):
        q, args = ("SELECT * FROM events WHERE change_id = ? ORDER BY seq", (change_id,)) if change_id \
            else ("SELECT * FROM events ORDER BY seq", ())
        return [dict(r) for r in self._run(lambda db: db.execute(q, args).fetchall())]

    def verify(self):
        """(True, n) if the chain is intact, else (False, seq of the first bad row)."""
        prev = "0" * 64
        rows = self.events()
        for r in rows:
            if r["prev_hash"] != prev or r["hash"] != _hash(prev, r["change_id"], r["ts"], r["stage"], r["result"], r["detail"]):
                return False, r["seq"]
            prev = r["hash"]
        return True, len(rows)
