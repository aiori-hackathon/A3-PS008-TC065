"""The 20 scenarios from the plan. 1-12 are the must-haves; together they cover the 4 PS categories:
conflicting routes, invalid configurations, rejected approvals, failed deployments."""
import time

from backend import generator, net, twin
from backend.engine import Engine

ADD = {"intent": "ADD_ROUTE", "router": "R1", "destination": "10.10.31.0/24", "next_hop": "10.0.13.2",
       "requested_by": "operator1", "reason": "R1 users need the R3 server LAN"}
REMOVE = {"intent": "REMOVE_ROUTE", "router": "R1", "destination": "10.10.30.128/25",
          "requested_by": "operator1", "reason": "legacy detour no longer needed"}


def intent(base=ADD, **kw):
    return {**base, **kw}


def approved(engine, raw, fault=None):
    c = engine.plan(raw)
    assert c["status"] == "PENDING_APPROVAL", c["reason"]
    return engine.apply(c["id"], "approver1", "looks good", fault)


# ---------- happy path ----------
def test_01_add_route_approved(engine, lab):
    c = approved(engine, ADD)
    assert c["status"] == "SUCCESS", c["reason"]
    assert ("10.10.31.0/24", "10.0.13.2") in lab.routes()["R1"]
    assert lab.ping("R1", "10.10.10.1", "10.10.31.1")


def test_02_remove_route_approved(engine, lab):
    c = approved(engine, REMOVE)
    assert c["status"] == "SUCCESS", c["reason"]
    assert ("10.10.30.128/25", "10.0.12.2") not in lab.routes()["R1"]
    assert c["candidate"] == {"R1": ["no ip route 10.10.30.128/25 10.0.12.2"]}  # next-hop looked up from R1


# ---------- invalid configurations ----------
def test_03_missing_field(engine):
    raw = dict(ADD)
    del raw["next_hop"]
    c = engine.plan(raw)
    assert c["status"] == "INVALID" and "next_hop" in c["reason"]


def test_04_invalid_ip_or_prefix(engine):
    for bad in [{"destination": "10.10.300.0/24"}, {"destination": "10.10.31.5/24"}, {"next_hop": "10.0.13"}]:
        c = engine.plan(intent(**bad))
        assert c["status"] == "INVALID", bad


def test_05_unknown_router(engine):
    c = engine.plan(intent(router="R99"))
    assert c["status"] == "INVALID" and "unknown router" in c["reason"]


def test_06_next_hop_not_connected(engine):
    c = engine.plan(intent(next_hop="10.0.23.2"))  # R2-R3 link, not on R1
    assert c["status"] == "BLOCKED" and c["stage"] == "semantic" and "not a directly connected" in c["reason"]


# ---------- conflicting routes ----------
def test_07_conflict_same_prefix_other_next_hop(engine):
    c = engine.plan(intent(destination="10.10.20.0/24", next_hop="10.0.13.2"))
    assert c["status"] == "BLOCKED" and "conflict" in c["reason"]


def test_08_loop_blocked_by_twin(engine):
    # live network: R2 sending the upper R3 LAN to R1, which sends it back to R2
    c = engine.plan(intent(router="R2", destination="10.10.30.128/25", next_hop="10.0.12.1"))
    assert c["status"] == "BLOCKED" and c["stage"] == "twin" and "R2 → R1 → R2" in c["reason"]
    # three-router loop on a plain table: R1 closing R2 → R3 → R1
    t = {"R1": set(), "R2": {("10.99.0.0/16", "10.0.23.2")}, "R3": {("10.99.0.0/16", "10.0.13.1")}}
    _, report = twin.validate(t, {"R1": ["ip route 10.99.0.0/16 10.0.12.2"]})
    assert not report[0]["ok"] and "R1 → R2 → R3 → R1" in report[0]["detail"]


# ---------- rejected approvals ----------
def test_09_human_rejects(engine, lab):
    before = lab.routes()
    c = engine.plan(ADD)
    c = engine.reject(c["id"], "approver1", "not in the change window")
    assert c["status"] == "REJECTED" and lab.routes() == before
    assert engine.apply(c["id"], "approver1")["status"] == "REJECTED"  # a rejected change can never deploy


def test_10_approval_timeout(lab, tmp_path):
    eng = Engine(lab, tmp_path / "a.db", tmp_path / "s", timeout=1)
    before = lab.routes()
    c = eng.plan(ADD)
    time.sleep(1.2)
    c = eng.apply(c["id"], "approver1", "too late")
    assert c["status"] == "REJECTED" and "timeout" in c["reason"] and lab.routes() == before


# ---------- failed deployments ----------
def test_11_deploy_fails_midway_all_rolled_back(engine, lab):
    before = lab.routes()
    c = approved(engine, ADD, fault="deploy")
    assert c["status"] == "ROLLED_BACK", c["reason"]
    assert "R3 rejected" in c["reason"] and lab.routes() == before  # R1 had already changed; it is restored too


def test_12_deploy_ok_but_pings_fail(engine, lab):
    before = lab.routes()
    c = approved(engine, ADD, fault="health")
    assert c["status"] == "ROLLED_BACK" and "ping failed" in c["reason"]
    assert lab.routes() == before and lab.ping("R2", "10.10.20.1", "10.10.30.1")


# ---------- extra scenarios ----------
def test_13_command_injection(engine):
    for bad in [{"destination": "10.10.31.0/24; reboot"}, {"next_hop": "10.0.13.2\nwrite erase"},
                {"reason": "ok; rm -rf /"}, {"requested_by": "op`id`"}, {"command": "write erase"}]:
        assert engine.plan(intent(**bad))["status"] == "INVALID", bad
    assert generator.not_allowed({"R1": ["ip route 10.10.31.0/24 10.0.13.2", "write erase"]}) == ["R1: 'write erase'"]


def test_14_duplicate_route(engine):
    c = engine.plan(intent(destination="10.10.20.0/24", next_hop="10.0.12.2"))
    assert c["status"] == "BLOCKED" and "duplicate" in c["reason"]


def test_15_remove_missing_route(engine):
    c = engine.plan(intent(REMOVE, destination="10.10.99.0/24"))
    assert c["status"] == "BLOCKED" and "nothing to remove" in c["reason"]


def test_16_invalid_intent_type(engine):
    assert engine.plan(intent(intent="DELETE_EVERYTHING"))["status"] == "INVALID"
    assert engine.plan({"router": "R1"})["status"] == "INVALID"


def test_17_blackhole_blocked_by_twin(engine):
    c = engine.plan(intent(destination="10.10.40.0/24", next_hop="10.0.12.2"))
    assert c["status"] == "BLOCKED" and "blackhole" in c["reason"] and "dropped at R2" in c["reason"]


def test_18_several_changes_in_a_row(engine, lab):
    a = approved(engine, intent(next_hop="R3"))  # router name is converted to R3's link IP
    b = approved(engine, intent(router="R2", next_hop="R3"))
    assert a["status"] == b["status"] == "SUCCESS" and a["id"] != b["id"]
    assert a["normalized"]["next_hop"] == "10.0.13.2" and b["normalized"]["next_hop"] == "10.0.23.2"
    assert lab.ping("R2", "10.10.20.1", "10.10.31.1")
    assert a["backup_id"] != b["backup_id"]


def test_19_rollback_matches_snapshot(engine, lab):
    c = approved(engine, ADD, fault="deploy")
    assert c["status"] == "ROLLED_BACK"
    for r in net.NAMES:
        assert lab.running_config(r) == (engine.snap_dir / c["id"] / f"{r}.conf").read_text(), r


def test_20_audit_log_complete_and_tamper_evident(engine):
    ok = approved(engine, ADD)
    bad = engine.plan(intent(router="R99"))
    rej = engine.reject(engine.plan(REMOVE)["id"], "approver1", "no")
    stages = lambda c: [e["stage"] for e in engine.db.events(c["id"])]
    assert stages(ok)[:4] == ["received", "schema", "semantic", "generate"]
    assert {"twin.loop", "twin.blackhole", "twin.reachability", "twin.syntax", "validation",
            "approval", "backup", "deploy", "health", "result"} <= set(stages(ok))
    assert stages(bad) == ["received", "schema"] and stages(rej)[-1] == "approval"
    assert engine.db.verify()[0]
    engine.db._run(lambda db: db.execute("UPDATE events SET detail = 'nothing happened' WHERE stage = 'deploy'"))
    assert engine.db.verify()[0] is False
