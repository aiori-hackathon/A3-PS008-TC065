"""HTTP API + dashboard. Run: .venv/bin/uvicorn backend.main:app --port 8000

Every /api route except login needs a signed-in user; the role each route needs is in its dependency.
Who requested or approved a change comes from the session, never from the request body.
"""
import json
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import auth, deploy, examples, labctl, labgen, nlp, schema, yangcheck
from backend.engine import db_path, default_engine
from backend.lab import LabError
from backend.topology import LABS, ROOT, Topology, TopologyError, lab_names, to_json

def _confirm_timer():
    """Unconfirmed changes roll back on time even when nobody has the dashboard open."""
    while True:
        time.sleep(5)
        try:
            eng().awaiting_confirm()
        except Exception as e:  # lab down or being redeployed: try again next tick
            print(f"confirm timer: {e}")


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=_confirm_timer, daemon=True, name="confirm-timer").start()
    yield


app = FastAPI(title="PS-008 Network Management Agent", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def fresh_dashboard(request, call_next):
    """The browser re-checks the dashboard files on every load (cheap, they have ETags), so after an update
    it never runs an old script against new HTML."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response
COOKIE = "nma_session"
_cache = {"key": None, "engine": None}

_first = auth.bootstrap(db_path(), ROOT / "data" / "initial-admin-password")
if _first:
    print(f"\n  First start: created user 'admin'. Password saved in data/initial-admin-password\n")


def eng():
    """Engine for the active lab, rebuilt when the active lab or its lab.yaml changes."""
    name = labctl.active_name()
    key = (name, (LABS / name / "lab.yaml").stat().st_mtime)
    if key != _cache["key"]:
        _cache.update(key=key, engine=default_engine(Topology.load(name)))
    return _cache["engine"]


def role(needed):
    def dep(request: Request):
        user = auth.session(request.cookies.get(COOKIE))
        if not user:
            raise HTTPException(401, "sign in first")
        if not auth.allowed(user, needed):
            raise HTTPException(403, f"this needs the {needed} role; {user['name']} is {user['role']}")
        return user
    return dep


OPERATOR, APPROVER, ADMIN = Depends(role("operator")), Depends(role("approver")), Depends(role("admin"))


def _call(fn, *args):
    try:
        return fn(*args)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'"))
    except ValueError as e:  # includes TopologyError and separation-of-duties refusals
        raise HTTPException(400, str(e))
    except LabError as e:
        raise HTTPException(503, f"lab unreachable: {e}")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


# ---------- sign in ----------
class Login(BaseModel):
    name: str
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    try:
        token, user = auth.login(db_path(), body.name, body.password)
    except auth.AuthError as e:
        time.sleep(0.5)  # slows down password guessing
        raise HTTPException(401, str(e))
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=auth.SESSION_HOURS * 3600)
    return user


@app.post("/api/logout")
def logout(request: Request, response: Response):
    auth.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE)
    return {"signed_out": True}


@app.get("/api/me")
def me(user=OPERATOR):
    return user


class NewUser(BaseModel):
    name: str
    role: Literal["operator", "approver", "admin"]
    password: str


@app.get("/api/users")
def list_users(user=ADMIN):
    return auth.users(db_path())


@app.post("/api/users")
def add_user(body: NewUser, user=ADMIN):
    _call(auth.add_user, db_path(), body.name, body.role, body.password)
    eng().db.event("user-" + uuid.uuid4().hex[:8], "user", "ADDED", f"{user['name']} added {body.name} as {body.role}")
    return {"added": body.name, "role": body.role}


# ---------- labs ----------
class NewLab(BaseModel):
    name: str
    template: Optional[str] = None      # one of labgen.TEMPLATES, or omit and send spec
    routers: int = 6
    seed: int = 7
    baseline: Literal["shortest-path", "none"] = "shortest-path"
    spec: Optional[dict] = None         # custom lab drawn in the builder: {routers: [...], links: [[a, b], ...]}


def _spec(body: NewLab):
    if body.spec is not None:
        spec = {**body.spec, "name": body.name, "template": "custom"}
    else:
        spec = labgen.template_spec(body.name, body.template or "ring", body.routers, body.seed)
    spec["baseline"] = body.baseline
    return spec


@app.get("/api/labs")
def labs(user=OPERATOR):
    try:
        live = labctl.running()
    except LabError:
        live = {}
    out = []
    for name in lab_names():
        try:
            t = Topology.load(name)
            out.append({"name": name, "template": t.template, "routers": len(t.names), "links": len(t.links),
                        "running": name in live, "active": name == labctl.active_name(),
                        "job": labctl.JOBS.get(name, {}).get("state")})
        except TopologyError as e:
            out.append({"name": name, "error": str(e)})
    return {"labs": out, "templates": labgen.TEMPLATES}


@app.post("/api/labs/preview")
def preview(body: NewLab, user=OPERATOR):
    def build():
        t = Topology(_spec(body))
        return {**t.summary(), "baseline_routes": to_json(t.baseline_routes())}
    return _call(build)


@app.post("/api/labs")
def create_lab(body: NewLab, user=ADMIN):
    if body.name in lab_names():
        raise HTTPException(409, f"a lab named {body.name} already exists")

    def build():
        t = Topology(_spec(body))
        t.save()
        return {"name": t.name, "routers": len(t.names), "links": len(t.links)}
    return _call(build)


@app.delete("/api/labs/{name}")
def delete_lab(name: str, user=ADMIN):
    if name == labctl.active_name() or name in labctl.running():
        raise HTTPException(400, f"{name} is active or running; stop it or switch labs first")
    if name not in lab_names():
        raise HTTPException(404, f"no lab {name}")
    shutil.rmtree(LABS / name)
    return {"deleted": name}


@app.put("/api/labs/{name}/layout")
def save_layout(name: str, layout: dict[str, list[float]], user=OPERATOR):
    def save():
        t = Topology.load(name)
        t.spec["layout"] = {r: [round(x), round(y)] for r, (x, y) in layout.items() if r in t.names}
        Topology(t.spec).save()
        return {"saved": len(t.spec["layout"])}
    return _call(save)


@app.post("/api/labs/{name}/deploy")
def deploy_lab(name: str, user=ADMIN):
    return _call(labctl.start_deploy, name)


@app.get("/api/labs/{name}/job")
def job(name: str, user=OPERATOR):
    return labctl.JOBS.get(name) or {"state": "none", "log": []}


@app.post("/api/labs/{name}/use")
def use_lab(name: str, user=ADMIN):
    if name not in labctl.running():
        raise HTTPException(400, f"lab {name} is not running; deploy it first")
    _call(labctl.set_active, name)
    return {"active": name}


@app.post("/api/labs/{name}/destroy")
def destroy_lab(name: str, user=ADMIN):
    _call(labctl.destroy, name)
    return {"destroyed": name}


# ---------- network ----------
@app.get("/api/network")
def network(user=OPERATOR):
    return _call(lambda: eng().network())


@app.get("/api/health")
def health(user=OPERATOR):
    def check():
        e = eng()
        ok, n = e.db.verify()
        return {**deploy.lab_health(e.lab, e.topo), "drift": e.drift(), "audit_intact": ok, "audit_events": n}
    return _call(check)


@app.get("/api/routers/{router}/config")
def router_config(router: str, user=OPERATOR):
    e = eng()
    if router not in e.topo.names:
        raise HTTPException(404, f"no router {router} in lab {e.topo.name}")
    return {"router": router, "config": _call(e.lab.running_config, router)}


@app.get("/api/examples")
def demo_examples(user=OPERATOR):
    return _call(lambda: examples.find(eng().lab.routes(), eng().topo))


# ---------- drift ----------
@app.post("/api/drift/revert")
def revert_drift(user=OPERATOR):
    """Creates a normal change (twin, approval, deploy, rollback) back to the approved state."""
    return _call(eng().plan_revert, user["name"])


@app.post("/api/drift/accept")
def accept_drift(user=ADMIN):
    return _call(eng().accept_drift, user["name"])


# ---------- plain language ----------
class Ask(BaseModel):
    text: str


@app.post("/api/ask")
def ask(body: Ask, user=OPERATOR):
    """Plain language -> proposed intents. Nothing is validated against the network or deployed here."""
    e = eng()
    result = nlp.understand(body.text, e.topo)
    e.db.event("ask-" + uuid.uuid4().hex[:8], "ask", "UNDERSTOOD" if result["intents"] else "NOT_UNDERSTOOD",
               json.dumps({"by": user["name"], "text": body.text[:500], "how": result["how"], "intents": result["intents"]},
                          ensure_ascii=False)[:1000])
    return result


# ---------- changes ----------
class Decision(BaseModel):
    reason: str = ""
    simulate_failure: Optional[Literal["deploy", "health"]] = None
    failure_router: Optional[str] = None
    confirm_timeout: Optional[int] = None  # seconds; set = confirmed commit (RFC 6241 8.4)


@app.post("/api/intent")
def submit(raw: dict, user=OPERATOR):
    """Stages 1-5. Returns the change, which is PENDING_APPROVAL only if every check passed."""
    return _call(eng().plan, {**raw, "requested_by": user["name"]})


@app.get("/api/changes")
def changes(user=OPERATOR):
    return eng().list()


@app.get("/api/changes/{change_id}")
def change(change_id: str, user=OPERATOR):
    c = eng().get(change_id)
    if not c:
        raise HTTPException(404, f"no change {change_id}")
    return {**c, "events": eng().db.events(change_id)}


@app.post("/api/changes/{change_id}/approve")
def approve(change_id: str, d: Decision, user=APPROVER):
    _yang(d, "approve-change")
    return _call(eng().apply, change_id, user["name"], d.reason, d.simulate_failure, d.failure_router, d.confirm_timeout)


@app.post("/api/changes/{change_id}/confirm")
def confirm(change_id: str, user=APPROVER):
    return _call(eng().confirm, change_id, user["name"])


@app.post("/api/changes/{change_id}/cancel")
def cancel(change_id: str, user=APPROVER):
    """cancel-commit: roll a change that is waiting for confirmation back now."""
    return _call(eng().cancel, change_id, user["name"])


@app.post("/api/changes/{change_id}/reject")
def reject(change_id: str, d: Decision, user=APPROVER):
    _yang({"reason": d.reason}, "reject-change")
    return _call(eng().reject, change_id, user["name"], d.reason)


def _yang(body, rpc):
    """Check a decision against the matching RPC of yang/nma-intent.yang. Empty values mean 'not given'."""
    doc = body if isinstance(body, dict) else body.model_dump()
    problem = yangcheck.check({k: v for k, v in doc.items() if v not in (None, "")}, rpc)
    if problem:
        raise HTTPException(400, f"not valid against nma-intent.yang: {problem}")


@app.get("/api/schema")
def intent_schema():
    return schema.json_schema()


@app.get("/api/yang", include_in_schema=False)
def yang_module():
    """The intent contract as a YANG 1.1 module (RFC 7950)."""
    return FileResponse(yangcheck.YANG / "nma-intent.yang", media_type="application/yang")
