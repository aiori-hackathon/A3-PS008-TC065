"""Stages 7-9: pre-flight, snapshot, deploy as one transaction, health check, rollback of all routers."""
import subprocess

from backend import twin


def fault(kind, topo, router=None):
    """Fault injection for tests and the demo. Bypasses the allowlist on purpose and only runs when asked.
    deploy: the router rejects its config midway.  health: config applies, then one of its links goes down."""
    router = router or topo.names[-1]
    if router not in topo.names:
        raise ValueError(f"unknown router {router} for failure test")
    if kind == "deploy":
        return router, ["ip route this-is-not-valid"]
    if kind == "health":
        link = next((i for i in topo.interfaces(router) if i != "lo"), None)
        if not link:
            raise ValueError(f"{router} has no links to take down")
        return router, [f"interface {link}", "shutdown"]
    raise ValueError(f"failure test must be 'deploy' or 'health', not {kind!r}")


def preflight(lab, topo):
    """Is the lab itself healthy? Every interface present, up and addressed; every link passes traffic.
    Catches a VM restart that brought the containers back without their links."""
    h = lab_health(lab, topo)
    if not h["ok"]:
        shown = "; ".join(h["problems"][:4]) + (f" (+{len(h['problems']) - 4} more)" if len(h["problems"]) > 4 else "")
        return False, f"lab is unhealthy before any change: {shown}. Redeploy the lab."
    return True, h["summary"]


def lab_health(lab, topo):
    """Structured health for the dashboard map: which routers and links are broken, and why."""
    states = lab.interface_states()
    problems, bad_ifaces = [], set()
    for r in topo.names:
        for ifname, addrs in topo.interfaces(r).items():
            st, want = states[r].get(ifname), {str(a) for a in addrs}
            issue = ("is missing" if st is None else "is down" if not st["up"]
                     else f"has no address {', '.join(sorted(want - set(st['ipv4'])))}" if want - set(st["ipv4"]) else None)
            if issue:
                problems.append(f"{r} {ifname} {issue}")
                bad_ifaces.add((r, ifname))
    testable = [i for i, l in enumerate(topo.links) if (l["a"], l["a_if"]) not in bad_ifaces and (l["b"], l["b_if"]) not in bad_ifaces]
    results = dict(zip(testable, lab.pings([(topo.links[i]["a"], topo.links[i]["a_ip"], topo.links[i]["b_ip"]) for i in testable])))
    bad_links = [i for i, l in enumerate(topo.links)
                 if (l["a"], l["a_if"]) in bad_ifaces or (l["b"], l["b_if"]) in bad_ifaces or not results[i]]
    problems += [f"link {topo.links[i]['a']}–{topo.links[i]['b']} ({topo.links[i]['subnet']}) passes no traffic"
                 for i in testable if not results[i]]
    n_if = sum(len(topo.interfaces(r)) for r in topo.names)
    return {"ok": not problems, "problems": problems, "bad_links": bad_links,
            "bad_routers": sorted({r for r, _ in bad_ifaces}, key=topo.names.index),
            "summary": f"{n_if} interfaces up, {len(topo.links)} links pass traffic" if not problems
            else f"{len(problems)} problem(s): " + "; ".join(problems[:3])}


def snapshot(lab, topo, snap_dir, change_id):
    """Save every router's running-config to snapshots/<id>/ and commit it to git. Returns (configs, commit)."""
    configs = {r: lab.running_config(r) for r in topo.names}
    folder = snap_dir / change_id
    folder.mkdir(parents=True, exist_ok=True)
    for r, text in configs.items():
        (folder / f"{r}.conf").write_text(text)
    git = ["git", "-C", str(snap_dir), "-c", "user.name=nma-agent", "-c", "user.email=nma-agent@localhost"]
    if not (snap_dir / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", change_id], check=True)
    subprocess.run([*git, "commit", "-q", "-m", f"snapshot before change {change_id} on lab {topo.name}"], check=True)
    commit = subprocess.run([*git, "rev-parse", "--short", "HEAD"], check=True, capture_output=True, text=True)
    return configs, commit.stdout.strip()


def deploy(lab, candidate, topo, failure=None, failure_router=None):
    """Apply to routers in lab order. Any router failing fails the whole change."""
    plan = {r: list(candidate.get(r, [])) for r in topo.names}
    if failure:
        router, lines = fault(failure, topo, failure_router)
        plan[router] += lines
    for r, lines in plan.items():
        if lines:
            ok, out = lab.configure(r, lines)
            if not ok:
                return False, f"{r} rejected the config: {out[:200]}"
    return True, f"{sum(1 for l in plan.values() if l)} router(s) accepted the config"


def expected_pings(routes, topo):
    """(src_router, src_ip, dst_ip) for every LAN pair the twin says works in both directions."""
    tables = twin._prepare(routes)
    pairs = []
    for s in topo.names:
        for d in topo.names:
            for lan in (topo.lans(d) if d != s else []):
                there = twin.walk(routes, s, lan.network, topo, tables)[0] == "DELIVERED"
                back = twin.walk(routes, d, topo.lan_net(s), topo, tables)[0] == "DELIVERED"
                if there and back:
                    pairs.append((s, topo.lan_ip(s), str(lan.ip)))
    return pairs


def health(lab, expected, topo):
    """Every interface and link still works, routes on every router equal the expected table,
    and every LAN pair the twin predicts can ping."""
    h = lab_health(lab, topo)
    if not h["ok"]:
        return False, "the lab is damaged: " + "; ".join(h["problems"][:4])
    actual = lab.routes()
    for r in topo.names:
        if actual[r] != expected[r]:
            missing = ", ".join(" via ".join(x) for x in sorted(expected[r] - actual[r])) or "none"
            extra = ", ".join(" via ".join(x) for x in sorted(actual[r] - expected[r])) or "none"
            return False, f"{r} routes differ from expected (missing: {missing}; unexpected: {extra})"
    jobs = expected_pings(expected, topo)
    failed = [f"{s} {src} → {dst}" for (s, src, dst), ok in zip(jobs, lab.pings(jobs)) if not ok]
    if failed:
        shown = ", ".join(failed[:6]) + (f" (+{len(failed) - 6} more)" if len(failed) > 6 else "")
        return False, f"ping failed: {shown}"
    return True, f"routes as expected on all routers, {len(jobs)} LAN pings passed"


def rollback(lab, configs):
    """Restore the snapshot on every router, not just the one that failed."""
    results = {r: lab.restore(r, text) for r, text in configs.items()}
    bad = [f"{r}: {out}" for r, (ok, out) in results.items() if not ok]
    return (not bad), ("; ".join(bad) if bad else "snapshot restored on " + ", ".join(configs))
