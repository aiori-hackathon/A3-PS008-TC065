"""Tests run against FakeLab (in-memory routers, seconds, no Docker) by default.
NMA_LIVE=1 runs the same tests against the real triangle-demo routers, reset to baseline before and after each test."""
import os

import pytest

from backend import twin
from backend.engine import Engine
from backend.generator import ALLOWED
from backend.topology import Topology, parse_static

LIVE = os.environ.get("NMA_LIVE") == "1"


class FakeLab:
    """Same interface as DockerLab. Pings are answered by walking the current routes."""

    def __init__(self, topo):
        self.topo = topo
        self.tables = topo.baseline_routes()
        self.down = set()      # routers with a shut interface (fault injection)
        self.cut = set()       # (router, ifname) missing, as after a VM restart

    def routes(self):
        return {r: set(t) for r, t in self.tables.items()}

    def running_config(self, r):
        lines = [f"ip route {p} {nh}" for p, nh in sorted(self.tables[r])]
        return "\n".join([f"hostname {r}", *lines, *(["interface x", " shutdown"] if r in self.down else []), "end"]) + "\n"

    def interface_states(self):
        return {r: {i: {"up": r not in self.down or i == "lo", "ipv4": [str(a) for a in addrs]}
                    for i, addrs in self.topo.interfaces(r).items() if (r, i) not in self.cut}
                for r in self.topo.names}

    def dry_run(self, r, lines):
        bad = [l for l in lines if not ALLOWED.fullmatch(l)]
        return (not bad), (f"% Unknown command: {bad[0]}" if bad else "FRR parser accepted the candidate")

    def configure(self, r, lines):
        for line in lines:
            if line.startswith("interface "):
                continue
            if line == "shutdown":
                self.down.add(r)
            elif ALLOWED.fullmatch(line):
                self.tables = twin.apply_candidate(self.tables, {r: [line]})
            else:
                return False, f"% Unknown command: {line}"
        return True, ""

    def restore(self, r, config):
        self.tables[r] = parse_static(config)
        (self.down.add if " shutdown" in config else self.down.discard)(r)
        return True, "restored"

    def ping(self, r, src, dst):
        dst_router = self.topo.owner(dst)
        if self.down & {r, dst_router} or any(c[0] in (r, dst_router) for c in self.cut):
            return False
        if self.topo.is_connected(r, f"{dst}/32"):  # link ping between neighbours
            return True
        dst_net = next(a.network for a in self.topo.lans(dst_router) if str(a.ip) == dst)
        return (twin.walk(self.tables, r, dst_net, self.topo)[0] == "DELIVERED"
                and twin.walk(self.tables, dst_router, self.topo.lan_net(r), self.topo)[0] == "DELIVERED")

    def pings(self, jobs):
        return [self.ping(*j) for j in jobs]


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch, tmp_path):
    """Tests never spend LLM credits: the key in .env is ignored and the cache lives in a temp folder.
    Tests that exercise the LLM set fake settings and a mock HTTP client themselves."""
    from backend import nlp
    monkeypatch.setenv("NMA_LLM_BASE_URL", "")
    monkeypatch.setattr(nlp, "CACHE", tmp_path / "llm_cache.json")


@pytest.fixture
def topo():
    return Topology.load("triangle-demo")


@pytest.fixture
def lab(topo, tmp_path):
    if not LIVE:
        yield FakeLab(topo)
        return
    from backend.lab import DockerLab
    real = DockerLab(topo)
    resetter = Engine(real, topo, tmp_path / "reset.db", None)
    resetter.reset()
    yield real
    resetter.reset()  # leave the lab clean for the dashboard


@pytest.fixture
def engine(lab, topo, tmp_path):
    return Engine(lab, topo, tmp_path / "audit.db", tmp_path / "snapshots", timeout=300)
