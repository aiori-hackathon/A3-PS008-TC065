"""The pipeline. plan() runs stages 1-5 and stops at the approval gate; apply() runs 6-9; reject() ends it.
The terminal (agent.py) and the API (main.py) both call these, and nothing else touches the routers.

Change.status: INVALID | BLOCKED | PENDING_APPROVAL | REJECTED | PENDING_CONFIRM | SUCCESS | ROLLED_BACK | ROLLBACK_FAILED

Confirmed commit (NETCONF, RFC 6241 section 8.4): an approver may deploy with a confirm timeout. The change is then
live but PENDING_CONFIRM; unless someone confirms it in time, every router is restored from the snapshot. cancel()
is cancel-commit: roll back now. While a change waits for confirmation no other change can start, because the
rollback restores the whole snapshot.
"""
import json
import re
import threading
import time
import uuid

from pydantic import ValidationError

from backend import checks, deploy, generator, labgen, schema, twin, yangcheck
from backend.topology import LABS, ROOT, from_json, net_of, to_json
from backend.audit import Audit
from backend.lab import LabError



class Engine:
    def __init__(self, lab, topo, db_path, snap_dir, timeout=300):
        self.lab, self.topo, self.db, self.snap_dir, self.timeout = lab, topo, Audit(db_path), snap_dir, timeout
        self.lock = threading.RLock()  # ponytail: one change at a time across the whole lab, which is the point

    # ---------- stages 1-5 ----------
    def plan(self, raw):
        c = {"id": uuid.uuid4().hex[:8], "created_at": time.time(), "status": "RECEIVED", "reason": "",
             "lab": self.topo.name, "intent": raw, "candidate": {}, "diff": {}, "report": [], "stage": "received"}
        self.db.event(c["id"], "received", "OK", json.dumps(raw, default=str)[:500])
        with self.lock:
            return self._plan(c)

    def _plan(self, c):
        try:
            intent = schema.parse(c["intent"], self.topo)
        except ValidationError as e:
            return self._stop(c, "INVALID", "schema", schema.errors(e))
        problem = yangcheck.check(c["intent"])
        if problem:
            return self._stop(c, "INVALID", "yang", problem)
        c["normalized"] = intent.model_dump(mode="json")
        self.db.event(c["id"], "schema", "PASS", "intent is well-formed and valid against the YANG model nma-intent (RFC 7950)")

        before = self._ready(c)
        if before is None:
            return c
        drift = _drift(before, self.approved()[0])
        if drift:
            return self._stop(c, "BLOCKED", "drift", f"the routers have changes made outside the agent ({_drift_text(drift)}); "
                                                     "revert or accept them first")

        error, ops = checks.semantic(intent, before, self.topo)
        if error:
            return self._stop(c, "BLOCKED", "semantic", error)
        self.db.event(c["id"], "semantic", "PASS", f"{len(ops)} route change(s) on {', '.join(dict.fromkeys(o[0] for o in ops))}")
        removed = (intent.router, str(intent.destination)) if intent.intent == "REMOVE_ROUTE" else None
        return self._validate(c, before, ops, removed)

    def _ready(self, c):
        """Read the routes and check the lab itself is healthy. Returns the routes, or None after stopping c."""
        open_ = self.awaiting_confirm()
        if open_:
            self._stop(c, "BLOCKED", "confirm", f"change {open_[0]['id']} is deployed but not confirmed yet; "
                                                "confirm or cancel it first, because its rollback restores the whole network")
            return None
        try:
            before = self.lab.routes()
            healthy, detail = deploy.preflight(self.lab, self.topo)
        except LabError as e:
            self._stop(c, "BLOCKED", "lab", f"cannot read routers: {e}")
            return None
        if not healthy:
            self._stop(c, "BLOCKED", "preflight", detail)
            return None
        self.db.event(c["id"], "preflight", "PASS", detail)
        c["before"] = to_json(before)
        return before

    def _validate(self, c, before, ops, removed=None, expect_loss=False):
        """Stages 3-5, shared by intents and drift reverts: generate, allowlist, twin, FRR syntax, then wait for a human."""
        candidate = generator.generate(ops)
        c["candidate"] = candidate
        bad = generator.not_allowed(candidate)
        if bad:
            return self._stop(c, "BLOCKED", "allowlist", f"command not allowed: {', '.join(bad)}")
        self.db.event(c["id"], "generate", "PASS", " | ".join(l for ls in candidate.values() for l in ls))

        after, report = twin.validate(before, candidate, self.topo, removed)
        if expect_loss:  # a revert goes back to the approved state; paths that only worked because of the drift go away
            report = [{**r, "ok": True, "detail": f"returning to the approved state: {r['detail']}"} if r["check"] == "reachability" and not r["ok"] else r
                      for r in report]
        for router, lines in candidate.items():
            try:
                ok, out = self.lab.dry_run(router, lines)
            except LabError as e:
                ok, out = False, str(e)
            report.append({"check": "syntax", "ok": ok, "detail": f"{router}: {out[:300]}"})
        c["report"], c["diff"] = report, _diff(before, after, candidate)
        for r in report:
            self.db.event(c["id"], f"twin.{r['check']}", "PASS" if r["ok"] else "FAIL", r["detail"])
        failed = [r for r in report if not r["ok"]]
        if failed:
            return self._stop(c, "BLOCKED", "twin", f"{failed[0]['check']} check failed: {failed[0]['detail']}")

        c.update(status="PENDING_APPROVAL", stage="approval", reason="SAFE TO REVIEW",
                 expires_at=c["created_at"] + self.timeout)
        self.db.event(c["id"], "validation", "PASS", "SAFE TO REVIEW, waiting for human approval")
        self.db.save(c)
        return c

    # ---------- stage 6: approval gate ----------
    def reject(self, change_id, approver, reason=""):
        with self.lock:
            c = self._pending(change_id)
            if c["status"] != "PENDING_APPROVAL":
                return c
            _check_person(approver, reason)
            c.update(approver=approver, approval_reason=reason)
            return self._stop(c, "REJECTED", "approval", f"rejected by {approver}: {reason or 'no reason given'}")

    # ---------- stages 7-9 ----------
    def apply(self, change_id, approver, reason="", simulate_failure=None, failure_router=None, confirm_timeout=None):
        with self.lock:
            c = self._pending(change_id)
            if c["status"] != "PENDING_APPROVAL":
                return c
            _check_person(approver, reason)
            if approver.lower() == str(c["intent"].get("requested_by", "")).lower():
                raise ValueError(f"separation of duties: {approver} requested this change and cannot also approve it")
            if c["lab"] != self.topo.name:
                raise ValueError(f"change {change_id} was planned on lab {c['lab']}, the active lab is {self.topo.name}")
            if simulate_failure:
                deploy.fault(simulate_failure, self.topo, failure_router)  # validates the failure test before anything runs
            if confirm_timeout is not None and not 10 <= int(confirm_timeout) <= 3600:
                raise ValueError("confirm timeout must be between 10 and 3600 seconds")
            c.update(approver=approver, approval_reason=reason, approved_at=time.time(),
                     simulate_failure=simulate_failure, failure_router=failure_router)
            self.db.event(c["id"], "approval", "APPROVED", f"approved by {approver}: {reason or 'no reason given'}")

            before = from_json(c["before"])
            after = twin.apply_candidate(before, c["candidate"])
            try:
                healthy, detail = deploy.preflight(self.lab, self.topo)
                if not healthy:
                    return self._stop(c, "BLOCKED", "preflight", detail + " Nothing was deployed.")
                if self.lab.routes() != before:
                    return self._stop(c, "BLOCKED", "revalidate", "network changed since validation, submit the intent again")
                configs, c["backup_id"] = deploy.snapshot(self.lab, self.topo, self.snap_dir, c["id"])
            except (LabError, OSError) as e:
                return self._stop(c, "BLOCKED", "backup", f"snapshot failed, nothing was deployed: {e}")
            self.db.event(c["id"], "backup", "PASS", f"snapshots/{c['id']}/ commit {c['backup_id']}")

            try:
                ok, detail = deploy.deploy(self.lab, c["candidate"], self.topo, simulate_failure, failure_router)
                self.db.event(c["id"], "deploy", "PASS" if ok else "FAIL", detail)
                if ok:
                    ok, detail = deploy.health(self.lab, after, self.topo)
                    self.db.event(c["id"], "health", "PASS" if ok else "FAIL", detail)
            except LabError as e:
                ok, detail = False, f"lab error during deploy: {e}"
            if not ok:
                return self._rollback(c, before, configs, "deploy", detail)
            self._set_approved(after, f"change {c['id']} approved by {approver}")
            if confirm_timeout:
                t = int(confirm_timeout)
                c.update(status="PENDING_CONFIRM", stage="confirm", reason=detail, confirm_timeout=t, confirm_by=time.time() + t)
                self.db.event(c["id"], "confirm", "WAITING", f"{detail}. Confirmed commit: rolls back automatically unless confirmed within {t}s")
                self.db.save(c)
                return c
            c.update(status="SUCCESS", stage="done", reason=detail)
            self.db.event(c["id"], "result", "SUCCESS", detail)
            self.db.save(c)
            return c

    # ---------- confirmed commit ----------
    def confirm(self, change_id, who):
        """Make a confirmed-commit change permanent. Like approval, never by the person who requested it."""
        with self.lock:
            c = self._pending(change_id)
            if c["status"] != "PENDING_CONFIRM":
                raise ValueError(f"change {change_id} is {c['status']}, not waiting for confirmation")
            _check_person(who, "")
            if who.lower() == str(c["intent"].get("requested_by", "")).lower():
                raise ValueError(f"separation of duties: {who} requested this change and cannot also confirm it")
            c.update(status="SUCCESS", stage="done", confirmed_by=who)
            self.db.event(c["id"], "confirm", "CONFIRMED", f"confirmed by {who}")
            self.db.event(c["id"], "result", "SUCCESS", c["reason"])
            self.db.save(c)
            return c

    def cancel(self, change_id, who):
        """cancel-commit: undo a change that is still waiting for confirmation, now."""
        with self.lock:
            c = self._pending(change_id)
            if c["status"] != "PENDING_CONFIRM":
                raise ValueError(f"change {change_id} is {c['status']}, not waiting for confirmation")
            _check_person(who, "")
            return self._undo(c, f"cancelled by {who}")

    def awaiting_confirm(self):
        """Changes on this lab that are live but not confirmed. Overdue ones are rolled back first."""
        return [c for c in map(self._expire, self.db.list()) if c["status"] == "PENDING_CONFIRM" and c["lab"] == self.topo.name]

    def _undo(self, c, why):
        configs = {r: (self.snap_dir / c["id"] / f"{r}.conf").read_text() for r in self.topo.names}
        return self._rollback(c, from_json(c["before"]), configs, "confirm", why)

    def _rollback(self, c, before, configs, stage, why):
        """Restore the snapshot on every router, verify, and record the result."""
        try:
            ok, detail = deploy.rollback(self.lab, configs)
            if ok:
                ok, detail = deploy.health(self.lab, before, self.topo)
        except LabError as e:
            ok, detail = False, str(e)
        self.db.event(c["id"], "rollback", "PASS" if ok else "FAIL", detail)
        if ok:
            self._set_approved(before, f"rollback of change {c['id']}")
        status = "ROLLED_BACK" if ok else "ROLLBACK_FAILED"
        alert = "" if ok else " OPERATOR ACTION NEEDED: network may be inconsistent."
        return self._stop(c, status, stage, f"{why}. Rollback: {detail}.{alert}")

    # ---------- reads ----------
    def get(self, change_id):
        c = self.db.get(change_id)
        return c and self._expire(c)

    def list(self):
        return [self._expire(c) for c in self.db.list()]

    def network(self):
        t, routes = self.topo, self.lab.routes()
        lans = sorted(t.all_lans(), key=net_of)
        reach = [{"src": s, "dst": d, "outcome": o, "path": p} for (s, d), (o, p) in sorted(twin.analyse(routes, lans, t).items())]
        summary = t.summary()
        for r, info in summary["routers"].items():
            info.update(connected=[str(x) for x in t.connected(r)], addresses=[str(a.ip) for a in t.addresses(r)],
                        static=sorted(map(list, routes[r]), key=lambda x: net_of(x[0])))
        return {**summary, "reachability": reach}

    def reset(self):
        """Put every router back to the lab's baseline config (generated from lab.yaml)."""
        routes = self.topo.baseline_routes()
        result = {r: self.lab.restore(r, labgen.frr_conf(self.topo, r, routes)) for r in self.topo.names}
        if all(ok for ok, _ in result.values()):
            self._set_approved(routes, "reset to baseline")
        return result

    # ---------- drift: changes made outside the agent ----------
    def _deployed_at(self):
        p = LABS / self.topo.name / "build" / "deployed_at"
        return p.read_text().strip() if p.exists() else ""

    def approved(self):
        """(routes, who/when) of the last state the agent put on the routers. A fresh deploy starts from the baseline."""
        row = self.db.get_approved(self.topo.name)
        if row and row["deployed_at"] == self._deployed_at():
            return from_json(json.loads(row["routes"])), {"by": row["by"], "at": row["at"]}
        return self.topo.baseline_routes(), {"by": "lab baseline", "at": None}

    def _set_approved(self, routes, by):
        self.db.set_approved(self.topo.name, self._deployed_at(), json.dumps(to_json(routes)), by)

    def drift(self):
        approved, meta = self.approved()
        d = _drift(self.lab.routes(), approved)
        return {"drift": bool(d), "routers": d, "summary": _drift_text(d) if d else "routers match the approved state",
                "approved_by": meta["by"]}

    def accept_drift(self, by):
        with self.lock:
            live = self.lab.routes()
            d = _drift(live, self.approved()[0])
            if not d:
                raise ValueError("there is no drift to accept")
            self._set_approved(live, f"drift accepted by {by}")
            self.db.event("drift-" + uuid.uuid4().hex[:8], "drift", "ACCEPTED", f"{by} accepted {_drift_text(d)}")
            return self.drift()

    def plan_revert(self, requested_by):
        """A normal change (twin, approval, deploy, rollback) that puts the routers back to the approved state."""
        c = {"id": uuid.uuid4().hex[:8], "created_at": time.time(), "status": "RECEIVED", "reason": "", "lab": self.topo.name,
             "intent": {"intent": "REVERT_DRIFT", "requested_by": requested_by, "reason": "return to the approved state"},
             "candidate": {}, "diff": {}, "report": [], "stage": "received"}
        self.db.event(c["id"], "received", "OK", json.dumps(c["intent"]))
        with self.lock:
            before = self._ready(c)
            if before is None:
                return c
            d = _drift(before, self.approved()[0])
            if not d:
                return self._stop(c, "BLOCKED", "drift", "there is no drift to revert")
            ops = [(r, "remove", p, nh) for r, x in d.items() for p, nh in x["unexpected"]] + \
                  [(r, "add", p, nh) for r, x in d.items() for p, nh in x["missing"]]
            self.db.event(c["id"], "semantic", "PASS", f"revert {_drift_text(d)}")
            return self._validate(c, before, ops, expect_loss=True)

    # ---------- helpers ----------
    def _stop(self, c, status, stage, reason):
        c.update(status=status, stage=stage, reason=f"{stage}: {reason}")
        self.db.event(c["id"], stage, status, reason)
        self.db.save(c)
        return c

    def _expire(self, c):
        if c["status"] == "PENDING_APPROVAL" and time.time() > c["expires_at"]:
            c.update(approver="system")
            return self._stop(c, "REJECTED", "approval", f"no decision within {self.timeout}s, timeout counts as reject")
        if c["status"] == "PENDING_CONFIRM" and time.time() > c["confirm_by"] and c["lab"] == self.topo.name:
            # a reader may get here while a deploy holds the lock: skip, the next read (or the server's timer) rolls back
            if self.lock.acquire(blocking=False):
                try:
                    c = self.db.get(c["id"])  # re-read under the lock; another thread may have confirmed it
                    if c["status"] == "PENDING_CONFIRM":
                        return self._undo(c, f"not confirmed within {c['confirm_timeout']}s")
                finally:
                    self.lock.release()
        return c

    def _pending(self, change_id):
        c = self.db.get(change_id)
        if not c:
            raise KeyError(f"no change {change_id}")
        return self._expire(c)


def _check_person(approver, reason):
    if not re.fullmatch(schema.SAFE_NAME, approver or ""):
        raise ValueError("approver name is required (letters, digits, . _ -)")
    if reason and not re.fullmatch(schema.SAFE_TEXT, reason):
        raise ValueError("reason contains characters that are not allowed")


def _drift(live, approved):
    """{router: {"unexpected": [(prefix, nh)], "missing": [...]}} for routers that differ."""
    out = {}
    for r in approved:
        extra, missing = sorted(live.get(r, set()) - approved[r]), sorted(approved[r] - live.get(r, set()))
        if extra or missing:
            out[r] = {"unexpected": extra, "missing": missing}
    return out


def _drift_text(d):
    parts = [f"{r}: " + ", ".join([f"+ ip route {p} {nh}" for p, nh in x["unexpected"]] + [f"- ip route {p} {nh}" for p, nh in x["missing"]])
             for r, x in d.items()]
    return "; ".join(parts)


def _diff(before, after, candidate):
    """Per changed router: every static route line marked '+', '-' or ' '."""
    out = {}
    for r in candidate:
        b, a = before[r], after[r]
        out[r] = [("+" if x not in b else "-" if x not in a else " ") + f" ip route {x[0]} {x[1]}"
                  for x in sorted(a | b, key=lambda x: (net_of(x[0]), x[1]))]
    return out


def db_path():
    """The SQLite file holding the audit log, approved states and users."""
    import os
    (ROOT / "data").mkdir(exist_ok=True)
    return os.environ.get("NMA_DB") or str(ROOT / "data" / "audit.db")


def default_engine(topo=None):
    """Engine for the active lab (labs/<name>/lab.yaml, chosen with `agent.py lab use` or the dashboard)."""
    import os
    from backend import labctl
    from backend.lab import DockerLab
    topo = topo or labctl.active_topology()
    return Engine(DockerLab(topo), topo, db_path(),
                  ROOT / os.environ.get("NMA_SNAPSHOTS", "snapshots"),
                  int(os.environ.get("NMA_APPROVAL_TIMEOUT", 300)))
