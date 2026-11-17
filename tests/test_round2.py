"""Round-2 scenarios: any topology, generated labs, CONNECT, plain language, pre-flight, drift, login and roles.
All run in seconds on FakeLab; nothing here needs the real routers."""
import json
import time

import httpx
import pytest

from backend import auth, deploy, examples, labgen, nlp, topology, twin
from backend.engine import Engine
from backend.topology import Topology, TopologyError
from tests.conftest import FakeLab

WHO = {"requested_by": "operator1", "reason": "test"}


def lab_of(template, n, baseline="shortest-path"):
    return Topology({**labgen.template_spec("t", template, n), "baseline": baseline})


def engine_for(topo, tmp_path, **kw):
    return Engine(FakeLab(topo), topo, tmp_path / "a.db", tmp_path / "s", **kw)


# ---------- topology and lab generation ----------
def test_21_addressing_is_automatic_and_unique():
    t = lab_of("partial-mesh", 30)
    nets = [str(a.network) for r in t.names for i, addrs in t.interfaces(r).items() for a in addrs if i == "lo"]
    nets += [l["subnet"] for l in t.links]
    assert len(nets) == len(set(nets))
    assert t.links[0] == {"a": "R1", "a_if": "eth1", "a_ip": "10.0.0.1", "b": "R2", "b_if": "eth1", "b_ip": "10.0.0.2", "subnet": "10.0.0.0/30"}
    assert Topology.load("triangle-demo").interfaces("R3")["eth2"][0].ip.compressed == "10.0.13.2"


@pytest.mark.parametrize("spec, error", [
    ({"routers": ["R1", "R2"], "links": [["R1", "R2"], ["R2", "R1"]]}, "duplicate link"),
    ({"routers": ["R1"], "links": [["R1", "R9"]]}, "two different routers"),
    ({"routers": ["R1", "r1"]}, "unique"),
    ({"routers": ["1bad"]}, "must start with a letter"),
    ({"routers": {"R1": {"lans": ["10.1.0.1/16"]}, "R2": {"lans": ["10.1.5.1/24"]}}}, "overlap"),
    ({"routers": [f"R{i}" for i in range(31)]}, "at most 30"),
])
def test_22_bad_topologies_are_refused(spec, error):
    with pytest.raises(TopologyError, match=error):
        Topology({"name": "bad", **spec})


@pytest.mark.parametrize("template", list(labgen.TEMPLATES))
def test_23_every_template_starts_fully_reachable(template):
    t = lab_of(template, 8 if template != "triangle" else 3)
    results = twin.analyse(t.baseline_routes(), t.all_lans(), t)
    assert results and all(o == "DELIVERED" for o, _ in results.values())


def test_24_labgen_writes_containerlab_and_frr_files(tmp_path, monkeypatch):
    monkeypatch.setattr(labgen, "LABS", tmp_path)
    t = lab_of("star", 4)
    clab = labgen.build(t).read_text()
    assert "r1:eth3" in clab and "quay.io/frrouting/frr" in clab and ":ro" in clab
    conf = (tmp_path / "t" / "build" / "configs" / "r2.conf").read_text()
    assert "interface eth1\n ip address 10.0.0.2/30" in conf and "ip route 10.10.3.0/24 10.0.0.1" in conf


# ---------- CONNECT ----------
def test_25_connect_builds_routes_along_the_path(tmp_path):
    t = lab_of("ring", 6, baseline="none")
    eng = engine_for(t, tmp_path)
    c = eng.plan({"intent": "CONNECT", "source": "R1", "destination": "R4", **WHO})
    assert c["status"] == "PENDING_APPROVAL", c["reason"]
    assert set(c["candidate"]) == {"R1", "R2", "R3", "R4"}  # R1 → R2 → R3 → R4 and back
    assert eng.apply(c["id"], "approver1")["status"] == "SUCCESS"
    assert eng.lab.ping("R1", t.lan_ip("R1"), t.lan_ip("R4"))


def test_26_connect_reuses_working_routes_and_refuses_no_ops(tmp_path):
    t = lab_of("line", 4, baseline="none")
    eng = engine_for(t, tmp_path)
    eng.apply(eng.plan({"intent": "CONNECT", "source": "R2", "destination": "R4", **WHO})["id"], "approver1")
    c = eng.plan({"intent": "CONNECT", "source": "R1", "destination": "R4", **WHO})
    assert "R3" not in c["candidate"] or not any("10.10.4.0" in l for l in c["candidate"]["R3"])  # R3 already delivers
    again = eng.plan({"intent": "CONNECT", "source": "R2", "destination": "R4", **WHO})
    assert again["status"] == "BLOCKED" and "nothing to do" in again["reason"]


def test_27_connect_blocks_on_a_conflicting_route(tmp_path):
    t = lab_of("line", 3, baseline="none")
    eng = engine_for(t, tmp_path)
    eng.lab.tables["R2"].add(("10.10.3.0/24", t.neighbour_ip("R2", "R1")))  # R2 sends R3's LAN the wrong way
    eng._set_approved(eng.lab.routes(), "test setup")
    c = eng.plan({"intent": "CONNECT", "source": "R1", "destination": "R3", **WHO})
    assert c["status"] == "BLOCKED" and "conflict" in c["reason"]


# ---------- plain language ----------
@pytest.mark.parametrize("text, expect", [
    ("connect R1 to R4", {"intent": "CONNECT", "source": "R1", "destination": "R4"}),
    ("make R4 reachable from R2", {"intent": "CONNECT", "source": "R2", "destination": "R4"}),
    ("R1 ko R3 tak pahunchao", {"intent": "CONNECT", "source": "R1", "destination": "R3"}),
    ("add route to 10.10.9.0/24 on R1 via R2", {"intent": "ADD_ROUTE", "router": "R1", "destination": "10.10.9.0/24", "next_hop": "R2"}),
    ("remove route 10.10.3.0/24 from R2", {"intent": "REMOVE_ROUTE", "router": "R2", "destination": "10.10.3.0/24"}),
])
def test_28_offline_grammar(text, expect):
    got = nlp.understand(text, lab_of("ring", 5))["intents"]
    assert got and all(got[0][k] == v for k, v in expect.items()), got


def test_29_offline_grammar_refuses_unsafe_or_unknown(monkeypatch):
    monkeypatch.setenv("NMA_LLM_BASE_URL", "")
    for text in ["reboot everything", "connect R1 to R99", "write erase on R1", ""]:
        assert nlp.understand(text, lab_of("ring", 5))["intents"] == [], text


def test_30_llm_output_is_validated_and_never_trusted(monkeypatch):
    monkeypatch.setenv("NMA_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("NMA_LLM_API_KEY", "k")
    monkeypatch.setenv("NMA_LLM_MODEL", "m")

    t = lab_of("ring", 5)
    # the model only translates; its lines still go through the grammar and the schema
    r = nlp.understand("मेरे R1 को R3 से जोड़ो", t, llm_client("connect R1 to R3\nadd route to 10.10.3.0/24 on R1 via R3"))
    assert [i["intent"] for i in r["intents"]] == ["CONNECT"] and "Dropped" in r["note"]  # R3 is not R1's neighbour
    assert r["intents"][0]["reason"] == "connect R1 to R3"
    assert nlp.understand("सब राउटर रीबूट करो", t, llm_client("NONE"))["intents"] == []
    assert nlp.understand("कुछ भी", t, llm_client("write erase\nreload"))["intents"] == []  # not a command form


def llm_client(content, calls=None, tokens=120):
    def handle(req):
        body = json.loads(req.content)
        if calls is not None:
            calls.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"total_tokens": tokens}})
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_41_llm_is_cheap_cached_and_capped(monkeypatch):
    monkeypatch.setenv("NMA_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("NMA_LLM_API_KEY", "k")
    monkeypatch.setenv("NMA_LLM_MODEL", "m")
    monkeypatch.setenv("NMA_LLM_DAILY_LIMIT", "2")
    t, calls = lab_of("ring", 5), []
    first = nlp.understand("R1 को R4 से जोड़ो", t, llm_client("connect R1 to R4", calls))
    again = nlp.understand("R1  को  R4 से जोड़ो", t, llm_client("connect R1 to R4", calls))  # same words, extra spaces
    assert first["intents"] == again["intents"] and len(calls) == 1 and again["usage"]["cached"]
    req = calls[0]
    assert req["max_tokens"] <= 60 and len(req["messages"][0]["content"]) < 600  # tiny prompt, tiny reply
    assert not any(lan in req["messages"][0]["content"] for lan in t.all_lans())  # the lab's addresses are never sent
    assert nlp.understand("connect R1 to R2", t, llm_client("x", calls))["usage"]["tokens"] == 0  # offline, no call
    nlp.understand("R2 को R5 से जोड़ो", t, llm_client("connect R2 to R5", calls))
    capped = nlp.understand("R3 को R5 से जोड़ो", t, llm_client("connect R3 to R5", calls))
    assert len(calls) == 2 and "limit of 2" in capped["note"] and nlp.usage() == {"calls": 2, "tokens": 240}


# ---------- safety ----------
def test_31_requester_cannot_approve_own_change(engine):
    c = engine.plan({"intent": "ADD_ROUTE", "router": "R1", "destination": "10.10.31.0/24", "next_hop": "R3", **WHO})
    with pytest.raises(ValueError, match="separation of duties"):
        engine.apply(c["id"], "OPERATOR1")
    assert engine.get(c["id"])["status"] == "PENDING_APPROVAL"


def test_32_preflight_refuses_a_broken_lab(topo, tmp_path):
    engine = engine_for(topo, tmp_path)
    lab = engine.lab
    c = engine.plan({"intent": "ADD_ROUTE", "router": "R1", "destination": "10.10.31.0/24", "next_hop": "R3", **WHO})
    lab.cut.add(("R1", "eth1"))  # what a VM restart did in the round-1 demo
    after = engine.apply(c["id"], "approver1")
    assert after["status"] == "BLOCKED" and after["stage"] == "preflight" and "R1 eth1 is missing" in after["reason"]
    assert engine.plan({"intent": "CONNECT", "source": "R1", "destination": "R3", **WHO})["stage"] == "preflight"


def test_33_a_dead_link_after_deploy_rolls_back_even_if_pings_pass(tmp_path):
    t = lab_of("ring", 5, baseline="none")
    eng = engine_for(t, tmp_path)
    c = eng.plan({"intent": "CONNECT", "source": "R1", "destination": "R2", **WHO})
    c = eng.apply(c["id"], "approver1", "", "health", "R4")  # R4's link is not on the R1-R2 path
    assert c["status"] == "ROLLED_BACK" and "R4 eth1 is down" in c["reason"]


def test_34_drift_blocks_changes_until_reverted(engine, lab):
    lab.configure("R2", ["ip route 10.99.0.0/24 10.0.12.1"])  # someone typed this on R2 by hand, outside the agent
    d = engine.drift()
    assert d["drift"] and "R2: + ip route 10.99.0.0/24" in d["summary"]
    blocked = engine.plan({"intent": "CONNECT", "source": "R1", "destination": "R3", **WHO})
    assert blocked["status"] == "BLOCKED" and blocked["stage"] == "drift"
    revert = engine.plan_revert("operator1")
    assert revert["candidate"] == {"R2": ["no ip route 10.99.0.0/24 10.0.12.1"]}
    assert engine.apply(revert["id"], "approver1")["status"] == "SUCCESS" and not engine.drift()["drift"]


def test_35_accepting_drift_is_recorded(engine, lab):
    lab.configure("R2", ["ip route 10.99.0.0/24 10.0.12.1"])
    engine.accept_drift("admin")
    assert not engine.drift()["drift"] and engine.approved()[1]["by"] == "drift accepted by admin"
    assert any(e["stage"] == "drift" and e["result"] == "ACCEPTED" for e in engine.db.events())


# ---------- login and roles ----------
def test_36_passwords_and_roles(tmp_path):
    db = tmp_path / "u.db"
    auth.add_user(db, "ana", "approver", "correct horse battery")
    with pytest.raises(auth.AuthError):
        auth.login(db, "ana", "wrong password!")
    with pytest.raises(auth.AuthError):
        auth.add_user(db, "bob", "operator", "short")
    token, user = auth.login(db, "ana", "correct horse battery")
    assert auth.session(token) == user and auth.allowed(user, "approver") and not auth.allowed(user, "admin")
    auth.logout(token)
    assert auth.session(token) is None


def test_37_api_takes_identity_from_the_session(tmp_path, monkeypatch, topo):
    from fastapi.testclient import TestClient
    from backend import main
    db = tmp_path / "api.db"
    eng = Engine(FakeLab(topo), topo, db, tmp_path / "s")
    monkeypatch.setattr(main, "eng", lambda: eng)
    monkeypatch.setattr(main, "db_path", lambda: db)
    for name, role in [("olga", "operator"), ("abe", "approver")]:
        auth.add_user(db, name, role, "a long enough password")

    def client(name):
        c = TestClient(main.app)
        assert c.post("/api/login", json={"name": name, "password": "a long enough password"}).status_code == 200
        return c
    assert TestClient(main.app).get("/api/network").status_code == 401
    olga, abe = client("olga"), client("abe")
    c = olga.post("/api/intent", json={"intent": "ADD_ROUTE", "router": "R1", "destination": "10.10.31.0/24",
                                       "next_hop": "R3", "requested_by": "abe", "reason": "spoof attempt"}).json()
    assert c["intent"]["requested_by"] == "olga"  # the body's requested_by is ignored
    assert olga.post(f"/api/changes/{c['id']}/approve", json={}).status_code == 403  # operators cannot approve
    assert olga.post("/api/labs/t/deploy").status_code == 403  # only admins run labs
    done = abe.post(f"/api/changes/{c['id']}/approve", json={"reason": "ok"}).json()
    assert done["status"] == "SUCCESS" and done["approver"] == "abe"


# ---------- examples and scale ----------
@pytest.mark.parametrize("template", ["ring", "tree", "partial-mesh"])
def test_38_generated_examples_do_what_they_say(template, tmp_path):
    t = lab_of(template, 7)
    eng = engine_for(t, tmp_path)
    want = {"add": "PENDING_APPROVAL", "remove": "PENDING_APPROVAL", "connect": "PENDING_APPROVAL",
            "loop": "BLOCKED", "blackhole": "BLOCKED", "conflict": "BLOCKED", "injection": "INVALID"}
    found = examples.find(eng.lab.routes(), t)
    assert {"add", "loop", "blackhole", "conflict", "injection"} <= {e["key"] for e in found}
    for ex in found:
        assert eng.plan(ex["intent"])["status"] == want[ex["key"]], ex


def test_39_twin_is_fast_on_a_30_router_lab():
    t = lab_of("partial-mesh", 30)
    start = time.time()
    _, report = twin.validate(t.baseline_routes(), {"R1": [f"ip route 192.0.2.0/24 {next(iter(t.neighbours('R1')))}"]}, t)
    assert time.time() - start < 1.0 and not report[1]["ok"]  # blackhole found, quickly


def test_40_health_check_uses_every_predicted_pair(tmp_path):
    t = lab_of("tree", 7)
    pairs = deploy.expected_pings(t.baseline_routes(), t)
    assert len(pairs) == 7 * 6
    ok, detail = deploy.health(FakeLab(t), t.baseline_routes(), t)
    assert ok and "42 LAN pings passed" in detail
