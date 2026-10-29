"""The only code that talks to routers: `docker exec <container> vtysh ...`.

On macOS the routers run inside the OrbStack machine `clab`, so commands go through
`orb -m clab docker`. Override with NMA_DOCKER (e.g. NMA_DOCKER=docker inside the VM).
"""
import os
import shlex
import subprocess
import sys

from backend import net

DOCKER = shlex.split(os.environ.get("NMA_DOCKER") or ("orb -m clab docker" if sys.platform == "darwin" else "docker"))


class LabError(RuntimeError):
    pass


class DockerLab:
    def _exec(self, router, *cmd, stdin=None, timeout=30):
        container = f"clab-{net.INV['lab']}-{net.ROUTERS[router]['node']}"
        try:
            # input="" so `docker exec -i` never inherits (and swallows) the operator's terminal input
            return subprocess.run([*DOCKER, "exec", "-i", container, *cmd], input=stdin or "",
                                  capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LabError(f"{router}: {e}") from e

    def _vtysh(self, router, *commands):
        p = self._exec(router, "vtysh", *[x for c in commands for x in ("-c", c)])
        out = (p.stdout + p.stderr).strip()
        if "No such container" in out or "Cannot connect" in out:
            raise LabError(f"{router}: {out}")
        return p.returncode == 0 and "% " not in out, out

    def running_config(self, router):
        ok, out = self._vtysh(router, "show running-config")
        if not ok:
            raise LabError(f"{router}: cannot read running-config: {out}")
        return out + "\n"

    def routes(self):
        return {r: net.parse_static(self.running_config(r)) for r in net.NAMES}

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
