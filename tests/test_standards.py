"""Standards the PS points to: YANG 1.1 (RFC 7950) for the intent model, and NETCONF's confirmed commit
(RFC 6241 section 8.4) for deployment. All run on FakeLab in seconds."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend import yangcheck
from backend.topology import ROOT

ADD = {"intent": "ADD_ROUTE", "router": "R1", "destination": "10.10.31.0/24", "next_hop": "10.0.13.2",
       "requested_by": "operator1", "reason": "R1 users need the R3 server LAN"}
NEW = ("10.10.31.0/24", "10.0.13.2")


def deployed_unconfirmed(engine, timeout=60):
    c = engine.plan(ADD)
    assert c["status"] == "PENDING_APPROVAL", c["reason"]
    c = engine.apply(c["id"], "approver1", "try it", confirm_timeout=timeout)
    assert c["status"] == "PENDING_CONFIRM", c["reason"]
    return c


# ---------- confirmed commit ----------
def test_42_confirmed_commit_stays_once_confirmed(engine, lab):
    c = deployed_unconfirmed(engine)
    assert NEW in lab.routes()["R1"]  # live while waiting, like NETCONF's confirmed commit
    with pytest.raises(ValueError, match="separation of duties"):
        engine.confirm(c["id"], "operator1")
    c = engine.confirm(c["id"], "approver1")
    assert c["status"] == "SUCCESS" and NEW in lab.routes()["R1"]
    assert not engine.drift()["drift"]
    stages = [(e["stage"], e["result"]) for e in engine.db.events(c["id"])]
    assert ("confirm", "WAITING") in stages and ("confirm", "CONFIRMED") in stages


def test_43_unconfirmed_change_rolls_back_by_itself(engine, lab):
    c = deployed_unconfirmed(engine)
    row = engine.db.get(c["id"])
    row["confirm_by"] = 0  # the confirm timeout has passed
    engine.db.save(row)
    c = engine.get(c["id"])  # any read (or the server's timer) notices
    assert c["status"] == "ROLLED_BACK" and "not confirmed within 60s" in c["reason"]
    assert NEW not in lab.routes()["R1"]
    assert not engine.drift()["drift"]  # the approved state went back with it
    with pytest.raises(ValueError, match="not waiting for confirmation"):
        engine.confirm(c["id"], "approver1")


def test_44_cancel_commit_and_no_new_change_while_waiting(engine, lab):
    c = deployed_unconfirmed(engine)
    other = engine.plan({**ADD, "router": "R2", "destination": "10.10.31.0/24", "next_hop": "10.0.23.2"})
    assert other["status"] == "BLOCKED" and c["id"] in other["reason"]  # its rollback would undo the newer change
    c = engine.cancel(c["id"], "approver1")
    assert c["status"] == "ROLLED_BACK" and "cancelled by approver1" in c["reason"]
    assert NEW not in lab.routes()["R1"]
    again = engine.plan(ADD)
    assert again["status"] == "PENDING_APPROVAL", again["reason"]  # free again


def test_45_confirm_timeout_is_bounded(engine):
    c = engine.plan(ADD)
    with pytest.raises(ValueError, match="between 10 and 3600"):
        engine.apply(c["id"], "approver1", confirm_timeout=5)
    assert engine.get(c["id"])["status"] == "PENDING_APPROVAL"  # nothing deployed


# ---------- YANG ----------
def test_46_yang_module_passes_pyang_strict_and_ietf_lint():
    pyang = shutil.which("pyang", path=str(Path(sys.executable).parent)) or shutil.which("pyang")
    if not pyang:
        pytest.skip("pyang not installed")
    out = subprocess.run([pyang, "--strict", "--lint", "-p", "yang", "yang/nma-intent.yang"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0 and not out.stderr.strip(), out.stderr


@pytest.mark.parametrize("doc, problem", [
    ({**ADD, "destination": "10.10.31.0/24; reboot"}, "destination"),                       # injection
    ({k: v for k, v in ADD.items() if k != "next_hop"}, "next_hop"),                        # mandatory when ADD_ROUTE
    ({**ADD, "intent": "REMOVE_ROUTE"}, "next_hop"),                                         # only allowed for ADD_ROUTE
    ({**ADD, "destination": "R3"}, "need a prefix"),                                         # must: routes need a prefix
    ({**ADD, "command": "reboot"}, "command"),                                               # unknown field
    ({**ADD, "intent": "DELETE_ALL"}, ""),                                                   # not an intent
    ({**ADD, "reason": "x; rm -rf /"}, "reason"),                                            # metacharacters
    ({"intent": "CONNECT", "source": "R1", "destination": "R3", "both_ways": "yes", "reason": "x"}, "both_ways"),
])
def test_47_yang_rejects_what_the_model_forbids(doc, problem):
    msg = yangcheck.check(doc)
    assert msg and problem in msg, msg


def test_48_yang_accepts_real_intents_and_the_pipeline_records_it(engine):
    for f in sorted((ROOT / "intents").glob("*.json")):  # the example requests in the repo
        doc = json.loads(f.read_text())
        assert (yangcheck.check(doc) is None) == ("injection" not in f.name), f.name
    assert yangcheck.check({"intent": "CONNECT", "source": "R1", "destination": "R3", "reason": "x"}) is None
    assert yangcheck.check({"confirm_timeout": 120, "reason": "ok"}, "approve-change") is None
    assert "confirm_timeout" in yangcheck.check({"confirm_timeout": 5}, "approve-change")
    c = engine.plan(ADD)
    assert any("YANG" in e["detail"] for e in engine.db.events(c["id"]) if e["stage"] == "schema")
