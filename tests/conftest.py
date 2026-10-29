"""Tests run against FakeLab (in-memory routers, seconds, no Docker) by default.
NMA_LIVE=1 runs the same tests against the real Containerlab routers, reset to baseline before each test."""
import os

import pytest

from backend import net, twin
from backend.engine import Engine
from backend.generator import ALLOWED

LIVE = os.environ.get("NMA_LIVE") == "1"
BASELINE = {r: net.parse_static((net.ROOT / "lab/configs" / net.ROUTERS[r]["node"] / "frr.conf").read_text())
            for r in net.NAMES}


class FakeLab:
    """Same interface as DockerLab. Pings are answered by walking the current routes."""

    def __init__(self):
        self.tables = {r: set(t) for r, t in BASELINE.items()}
        self.down = set()

    def routes(self):
        return {r: set(t) for r, t in self.tables.items()}

    def running_config(self, r):
        lines = [f"ip route {p} {nh}" for p, nh in sorted(self.tables[r])]
        return "\n".join([f"hostname {r}", *lines, *(["interface x", " shutdown"] if r in self.down else []), "end"]) + "\n"

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
        self.tables[r] = net.parse_static(config)
        (self.down.add if " shutdown" in config else self.down.discard)(r)
        return True, "restored"

    def ping(self, r, src, dst):
        dst_net = next(a.network for d in net.NAMES for a in net.lans(d) if str(a.ip) == dst)
        dst_router = net.owner(dst)
        if self.down & {r, dst_router}:
            return False
        return (twin.walk(self.tables, r, dst_net)[0] == "DELIVERED"
                and twin.walk(self.tables, dst_router, net.lan_net(r))[0] == "DELIVERED")


@pytest.fixture
def lab():
    if not LIVE:
        return FakeLab()
    from backend.lab import DockerLab
    real = DockerLab()
    Engine(real, ":memory:", None).reset()
    return real


@pytest.fixture
def engine(lab, tmp_path):
    return Engine(lab, tmp_path / "audit.db", tmp_path / "snapshots", timeout=300)
