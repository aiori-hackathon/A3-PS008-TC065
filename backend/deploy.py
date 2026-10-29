"""Stages 7-9: snapshot, deploy as one transaction, health check, rollback of all routers."""
import subprocess

from backend import net, twin

# Fault injection for tests and the demo. These bypass the allowlist on purpose and only run when asked.
_last = net.NAMES[-1]
_link = next(i for i in net.interfaces(_last) if i != "lo")
FAULTS = {
    "deploy": (_last, ["ip route this-is-not-valid"]),  # last router rejects its config midway
    "health": (_last, [f"interface {_link}", "shutdown"]),  # config applies, then a link dies, pings fail
}


def snapshot(lab, snap_dir, change_id):
    """Save every router's running-config to snapshots/<id>/ and commit it to git. Returns (configs, commit)."""
    configs = {r: lab.running_config(r) for r in net.NAMES}
    folder = snap_dir / change_id
    folder.mkdir(parents=True, exist_ok=True)
    for r, text in configs.items():
        (folder / f"{r}.conf").write_text(text)
    git = ["git", "-C", str(snap_dir), "-c", "user.name=nma-agent", "-c", "user.email=nma-agent@localhost"]
    if not (snap_dir / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", change_id], check=True)
    subprocess.run([*git, "commit", "-q", "-m", f"snapshot before change {change_id}"], check=True)
    commit = subprocess.run([*git, "rev-parse", "--short", "HEAD"], check=True, capture_output=True, text=True)
    return configs, commit.stdout.strip()


def deploy(lab, candidate, fault=None):
    """Apply to routers in inventory order. Any router failing fails the whole change."""
    plan = {r: list(candidate.get(r, [])) for r in net.NAMES}
    if fault:
        router, lines = FAULTS[fault]
        plan[router] += lines
    for r, lines in plan.items():
        if lines:
            ok, out = lab.configure(r, lines)
            if not ok:
                return False, f"{r} rejected the config: {out[:200]}"
    return True, "all routers accepted the config"


def expected_pings(routes):
    """(src_router, src_ip, dst_ip) for every LAN pair the twin says works in both directions."""
    pairs = []
    for s in net.NAMES:
        for d in net.NAMES:
            for lan in (net.lans(d) if d != s else []):
                there = twin.walk(routes, s, lan.network)[0] == "DELIVERED"
                back = twin.walk(routes, d, net.lan_net(s))[0] == "DELIVERED"
                if there and back:
                    pairs.append((s, net.ROUTERS[s]["lan_ip"], str(lan.ip)))
    return pairs


def health(lab, expected):
    """Routes on every router equal the expected table, and every LAN pair the twin predicts can ping."""
    actual = lab.routes()
    for r in net.NAMES:
        if actual[r] != expected[r]:
            missing = ", ".join(" via ".join(x) for x in sorted(expected[r] - actual[r])) or "none"
            extra = ", ".join(" via ".join(x) for x in sorted(actual[r] - expected[r])) or "none"
            return False, f"{r} routes differ from expected (missing: {missing}; unexpected: {extra})"
    pings = expected_pings(expected)
    failed = [f"{s} {src} → {dst}" for s, src, dst in pings if not lab.ping(s, src, dst)]
    if failed:
        return False, f"ping failed: {', '.join(failed)}"
    return True, f"routes as expected on all routers, {len(pings)} LAN pings passed"


def rollback(lab, configs):
    """Restore the snapshot on every router, not just the one that failed."""
    results = {r: lab.restore(r, text) for r, text in configs.items()}
    bad = [f"{r}: {out}" for r, (ok, out) in results.items() if not ok]
    return (not bad), ("; ".join(bad) if bad else "snapshot restored on " + ", ".join(configs))
