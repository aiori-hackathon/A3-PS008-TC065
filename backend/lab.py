"""The only code that talks to routers: `docker exec <container> vtysh ...`.

On macOS the routers run inside the OrbStack machine `clab`, so commands go through
`orb -m clab docker`. Override with NMA_DOCKER (e.g. NMA_DOCKER=docker inside the VM).
Reads and pings across routers run in parallel.
"""
import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from backend.topology import parse_static

DOCKER = shlex.split(os.environ.get("NMA_DOCKER") or ("orb -m clab docker" if sys.platform == "darwin" else "docker"))
VM = DOCKER[:-1]  # prefix that runs a command where Docker lives ([] when already there)
POOL = ThreadPoolExecutor(max_workers=16)


class LabError(RuntimeError):
    pass


def run(cmd, stdin=None, timeout=30):
    try:
        # input="" so `docker exec -i` never inherits (and swallows) the operator's terminal input
        return subprocess.run(cmd, input=stdin or "", capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LabError(str(e)) from e


class DockerLab:
    def __init__(self, topo):
        self.topo = topo

    def _exec(self, router, *cmd, stdin=None, timeout=30):
        p = run([*DOCKER, "exec", "-i", self.topo.container(router), *cmd], stdin, timeout)
        if p.returncode != 0 and ("No such container" in p.stderr or "is not running" in p.stderr or "Cannot connect" in p.stderr):
            raise LabError(f"{router}: {p.stderr.strip()} (is lab {self.topo.name} deployed?)")
        return p

    def _vtysh(self, router, *commands):
        p = self._exec(router, "vtysh", *[x for c in commands for x in ("-c", c)])
        out = (p.stdout + p.stderr).strip()
        return p.returncode == 0 and "% " not in out, out

    def running_config(self, router):
        ok, out = self._vtysh(router, "show running-config")
        if not ok:
            raise LabError(f"{router}: cannot read running-config: {out}")
        return out + "\n"

    def routes(self):
        configs = POOL.map(self.running_config, self.topo.names)
        return {r: parse_static(c) for r, c in zip(self.topo.names, configs)}

    def interface_states(self):
        """{router: {ifname: {"up": bool, "ipv4": ["10.0.12.1/30", ...]}}}"""
        def one(router):
            p = self._exec(router, "ip", "-j", "addr", "show")
            try:
                data = json.loads(p.stdout or "[]")
            except json.JSONDecodeError as e:
                raise LabError(f"{router}: cannot read interfaces: {p.stderr.strip()}") from e
            return {i["ifname"]: {"up": "UP" in i["flags"] and (i["ifname"] == "lo" or "LOWER_UP" in i["flags"]),
                                  "ipv4": [f"{a['local']}/{a['prefixlen']}" for a in i.get("addr_info", []) if a["family"] == "inet"]}
                    for i in data}
        return dict(zip(self.topo.names, POOL.map(one, self.topo.names)))

    def dry_run(self, router, lines):
        """FRR's own parser judges the candidate (vtysh --dryrun) without changing anything."""
        p = self._exec(router, "sh", "-c", "cat > /tmp/candidate.conf && vtysh -C -f /tmp/candidate.conf",
                       stdin="\n".join(lines) + "\n")
        return p.returncode == 0, (p.stdout + p.stderr).strip() or "FRR parser accepted the candidate"

    def configure(self, router, lines):
        return self._vtysh(router, "configure terminal", *lines)

    def restore(self, router, config):
        """Make the running-config equal `config` (frr-reload applies only the difference)."""
        body = "\n".join(l for l in config.splitlines() if l not in ("Building configuration...", "Current configuration:"))
        p = self._exec(router, "sh", "-c", "cat > /tmp/restore.conf && /usr/lib/frr/frr-reload.py --reload /tmp/restore.conf",
                       stdin=body + "\n", timeout=60)
        return p.returncode == 0, (p.stdout + p.stderr).strip()[-500:] or "restored"

    def ping(self, router, src, dst):
        for _ in range(2):  # one retry: an interface that was just re-enabled can drop the first packet
            if self._exec(router, "ping", "-c1", "-W1", "-I", src, dst).returncode == 0:
                return True
        return False

    def pings(self, jobs):
        """[(router, src_ip, dst_ip), ...] -> [bool, ...], run in parallel."""
        return list(POOL.map(lambda j: self.ping(*j), jobs))
