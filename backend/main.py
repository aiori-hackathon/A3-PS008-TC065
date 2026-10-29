"""HTTP API + dashboard. Run: .venv/bin/uvicorn backend.main:app --port 8000"""
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend import schema
from backend.engine import default_engine
from backend.lab import LabError

app = FastAPI(title="PS-008 Network Management Agent")
engine = default_engine()
INDEX = Path(__file__).parent / "static" / "index.html"


class Decision(BaseModel):
    approver: str
    reason: str = ""
    simulate_failure: Optional[Literal["deploy", "health"]] = None


def _call(fn, *args):
    try:
        return fn(*args)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LabError as e:
        raise HTTPException(503, f"lab unreachable: {e}")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX)


@app.get("/api/routers")
def routers():
    return _call(engine.network)


@app.post("/api/intent")
def submit(raw: dict):
    """Stages 1-5. Returns the change, which is PENDING_APPROVAL only if every check passed."""
    return _call(engine.plan, raw)


@app.get("/api/changes")
def changes():
    return engine.list()


@app.get("/api/changes/{change_id}")
def change(change_id: str):
    c = engine.get(change_id)
    if not c:
        raise HTTPException(404, f"no change {change_id}")
    return {**c, "events": engine.db.events(change_id)}


@app.post("/api/changes/{change_id}/approve")
def approve(change_id: str, d: Decision):
    return _call(engine.apply, change_id, d.approver, d.reason, d.simulate_failure)


@app.post("/api/changes/{change_id}/reject")
def reject(change_id: str, d: Decision):
    return _call(engine.reject, change_id, d.approver, d.reason)


@app.get("/api/schema")
def intent_schema():
    return schema.json_schema()


@app.get("/api/audit/verify")
def verify():
    ok, n = engine.db.verify()
    return {"intact": ok, "events" if ok else "first_bad_event": n}
