"""The pipeline. plan() runs stages 1-5 and stops at the approval gate; apply() runs 6-9; reject() ends it.
The terminal (agent.py) and the API (main.py) both call these, and nothing else touches the routers.

Change.status: INVALID | BLOCKED | PENDING_APPROVAL | REJECTED | SUCCESS | ROLLED_BACK | ROLLBACK_FAILED
"""
import json
import re
import threading
import time
import uuid
from ipaddress import IPv4Network

from pydantic import ValidationError

from backend import checks, deploy, generator, net, schema, twin
from backend.audit import Audit
from backend.lab import LabError



class Engine:
    def __init__(self, lab, db_path, snap_dir, timeout=300):
        self.lab, self.db, self.snap_dir, self.timeout = lab, Audit(db_path), snap_dir, timeout
        self.lock = threading.Lock()  # ponytail: one change at a time across the whole lab, which is the point

    # ---------- stages 1-5 ----------
    def plan(self, raw):
        c = {"id": uuid.uuid4().hex[:8], "created_at": time.time(), "status": "RECEIVED", "reason": "",
             "intent": raw, "candidate": {}, "diff": {}, "report": [], "stage": "received"}
        self.db.event(c["id"], "received", "OK", json.dumps(raw, default=str)[:500])
        with self.lock:
            return self._plan(c)

    def _plan(self, c):
        try:
            intent = schema.parse(c["intent"])
        except ValidationError as e:
            return self._stop(c, "INVALID", "schema", schema.errors(e))
        c["normalized"] = intent.model_dump(mode="json")
        self.db.event(c["id"], "schema", "PASS", "intent is well-formed")

        try:
            before = self.lab.routes()
        except LabError as e:
            return self._stop(c, "BLOCKED", "lab", f"cannot read routers: {e}")
        c["before"] = net.to_json(before)

        error, next_hops = checks.semantic(intent, before)
        if error:
            return self._stop(c, "BLOCKED", "semantic", error)
        self.db.event(c["id"], "semantic", "PASS", f"next-hops: {', '.join(next_hops)}")

        candidate = generator.generate(intent, next_hops)
        c["candidate"] = candidate
        bad = generator.not_allowed(candidate)
        if bad:
            return self._stop(c, "BLOCKED", "allowlist", f"command not allowed: {', '.join(bad)}")
        self.db.event(c["id"], "generate", "PASS", " | ".join(l for ls in candidate.values() for l in ls))

        removed = (intent.router, str(intent.destination)) if intent.intent == "REMOVE_ROUTE" else None
        after, report = twin.validate(before, candidate, removed)
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
    def apply(self, change_id, approver, reason="", simulate_failure=None):
        with self.lock:
            c = self._pending(change_id)
            if c["status"] != "PENDING_APPROVAL":
                return c
            _check_person(approver, reason)
            if simulate_failure not in (None, *deploy.FAULTS):
                raise ValueError(f"simulate_failure must be one of {list(deploy.FAULTS)}")
            c.update(approver=approver, approval_reason=reason, approved_at=time.time(), simulate_failure=simulate_failure)
            self.db.event(c["id"], "approval", "APPROVED", f"approved by {approver}: {reason or 'no reason given'}")

            before = net.from_json(c["before"])
            after = twin.apply_candidate(before, c["candidate"])
            try:
                if self.lab.routes() != before:
                    return self._stop(c, "BLOCKED", "revalidate", "network changed since validation, submit the intent again")
                configs, c["backup_id"] = deploy.snapshot(self.lab, self.snap_dir, c["id"])
            except (LabError, OSError) as e:
                return self._stop(c, "BLOCKED", "backup", f"snapshot failed, nothing was deployed: {e}")
            self.db.event(c["id"], "backup", "PASS", f"snapshots/{c['id']}/ commit {c['backup_id']}")

            try:
                ok, detail = deploy.deploy(self.lab, c["candidate"], simulate_failure)
                self.db.event(c["id"], "deploy", "PASS" if ok else "FAIL", detail)
                if ok:
                    ok, detail = deploy.health(self.lab, after)
                    self.db.event(c["id"], "health", "PASS" if ok else "FAIL", detail)
            except LabError as e:
                ok, detail = False, f"lab error during deploy: {e}"
            if ok:
                c.update(status="SUCCESS", stage="done", reason=detail)
                self.db.event(c["id"], "result", "SUCCESS", detail)
                self.db.save(c)
                return c

            try:
                rb_ok, rb_detail = deploy.rollback(self.lab, configs)
                if rb_ok:
                    rb_ok, rb_detail = deploy.health(self.lab, before)
            except LabError as e:
                rb_ok, rb_detail = False, str(e)
            self.db.event(c["id"], "rollback", "PASS" if rb_ok else "FAIL", rb_detail)
            status = "ROLLED_BACK" if rb_ok else "ROLLBACK_FAILED"
            alert = "" if rb_ok else " OPERATOR ACTION NEEDED: network may be inconsistent."
            return self._stop(c, status, "deploy", f"{detail}. Rollback: {rb_detail}.{alert}")

    # ---------- reads ----------
    def get(self, change_id):
        c = self.db.get(change_id)
        return c and self._expire(c)

    def list(self):
        return [self._expire(c) for c in self.db.list()]

    def network(self):
        routes = self.lab.routes()
        lans = sorted({str(a.network) for r in net.NAMES for a in net.lans(r)}, key=IPv4Network)
        reach = [{"src": s, "dst": d, "outcome": o, "path": p} for (s, d), (o, p) in sorted(twin.analyse(routes, lans).items())]
        return {"routers": {r: {"node": net.ROUTERS[r]["node"], "connected": [str(x) for x in net.connected(r)],
                                "addresses": [str(a.ip) for a in net.addresses(r)],
                                "static": sorted(map(list, routes[r]), key=lambda x: IPv4Network(x[0]))} for r in net.NAMES},
                "reachability": reach}

    def reset(self):
        """Put every router back to its baseline config from lab/configs/."""
        return {r: self.lab.restore(r, (net.ROOT / "lab/configs" / net.ROUTERS[r]["node"] / "frr.conf").read_text())
                for r in net.NAMES}

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


def _diff(before, after, candidate):
    """Per changed router: every static route line marked '+', '-' or ' '."""
    out = {}
    for r in candidate:
        b, a = before[r], after[r]
        out[r] = [("+" if x not in b else "-" if x not in a else " ") + f" ip route {x[0]} {x[1]}"
                  for x in sorted(a | b, key=lambda x: (IPv4Network(x[0]), x[1]))]
    return out


def default_engine():
    import os
    from backend.lab import DockerLab
    data = net.ROOT / "data"
    data.mkdir(exist_ok=True)
    return Engine(DockerLab(), os.environ.get("NMA_DB", data / "audit.db"),
                  net.ROOT / os.environ.get("NMA_SNAPSHOTS", "snapshots"),
                  int(os.environ.get("NMA_APPROVAL_TIMEOUT", 300)))
