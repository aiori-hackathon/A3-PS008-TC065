"""Stage 4: digital twin. A Python model of the lab; the candidate is applied to a copy only.

For every destination, start at every router and follow next-hops (longest prefix match):
  DELIVERED  reached a router where the destination is directly connected
  LOOP       visited the same router twice
  BLACKHOLE  forwarded, then reached a router with no route (or a next-hop that is not a lab router)
  NO_ROUTE   the source itself has no route (plain unreachable, not a fault on its own)
The candidate fails if it creates a new loop, a new blackhole, or breaks reachability that existed before.

ponytail: LANs live on lo, where Linux treats the whole subnet as local, so "connected wins" matches the
real routers. LANs on real interfaces would need connected routes inside the longest-prefix match.
"""
from backend.topology import net_of


def _prepare(routes):
    """Per router: routes sorted longest prefix first, so the first match is the LPM."""
    return {r: sorted(((net_of(p), nh) for p, nh in t), key=lambda x: -x[0].prefixlen) for r, t in routes.items()}


def walk(routes, src, dst, topo, prepared=None):
    tables = prepared if prepared is not None else _prepare(routes)
    dst, path = net_of(str(dst)), [src]
    while True:
        at = path[-1]
        if topo.is_connected(at, dst):
            return "DELIVERED", path
        nh = next((nh for p, nh in tables.get(at, ()) if dst.subnet_of(p)), None)
        if nh is None:
            return ("NO_ROUTE" if at == src else "BLACKHOLE"), path
        nxt = topo.owner(nh)
        if nxt is None:
            return "BLACKHOLE", path
        if nxt in path:
            return "LOOP", path + [nxt]
        path.append(nxt)


def destinations(routes, topo):
    """Every LAN plus every prefix any router has a static route for (so /25 detours are walked too)."""
    return set(topo.all_lans()) | {p for table in routes.values() for p, _ in table}


def analyse(routes, dests, topo):
    tables = _prepare(routes)
    return {(s, d): walk(routes, s, d, topo, tables) for d in dests for s in topo.names if not topo.is_connected(s, d)}


def apply_candidate(routes, candidate):
    new = {r: set(t) for r, t in routes.items()}
    for router, lines in candidate.items():
        for line in lines:
            _, _, prefix, nh = line.removeprefix("no ").split()
            (new[router].discard if line.startswith("no ") else new[router].add)((prefix, nh))
    return new


def _arrow(path):
    return " → ".join(path)


def _check(name, problems, ok_text):
    shown = "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else "")
    return {"check": name, "ok": not problems, "detail": shown or ok_text}


def validate(before, candidate, topo, removed=None):
    """removed=(router, prefix) for REMOVE_ROUTE: losing that router's own path to it is the point of the change."""
    after = apply_candidate(before, candidate)
    dests = destinations(before, topo) | destinations(after, topo)
    b, a = analyse(before, dests, topo), analyse(after, dests, topo)

    loops = [f"{d} from {s}: {_arrow(p)}" for (s, d), (o, p) in sorted(a.items())
             if o == "LOOP" and b[(s, d)][0] != "LOOP"]
    holes = [f"{d} from {s}: dropped at {p[-1]} ({_arrow(p)})" for (s, d), (o, p) in sorted(a.items())
             if o == "BLACKHOLE" and b[(s, d)][0] != "BLACKHOLE"]
    lost = [f"{s} can no longer reach {d} ({a[(s, d)][0].lower().replace('_', ' ')})"
            for (s, d), (o, _) in sorted(b.items())
            if o == "DELIVERED" and a[(s, d)][0] != "DELIVERED"
            and not (removed and s == removed[0] and net_of(d).subnet_of(net_of(removed[1])))]

    return after, [
        _check("loop", loops, "no new routing loops"),
        _check("blackhole", holes, "no new blackholes"),
        _check("reachability", lost, "every path that worked before still works"),
    ]


if __name__ == "__main__":
    # Self-check: R2 and R3 already point 10.99.0.0/16 around the triangle; R1 closing it is a loop.
    from backend.topology import Topology
    topo = Topology({"name": "selfcheck", "routers": ["R1", "R2", "R3"], "links": [["R1", "R2"], ["R2", "R3"], ["R1", "R3"]]})
    to = {r: {n: ip for ip, n in topo.neighbours(r).items()} for r in topo.names}
    t = {"R1": set(), "R2": {("10.99.0.0/16", to["R2"]["R3"])}, "R3": {("10.99.0.0/16", to["R3"]["R1"])}}
    _, rep = validate(t, {"R1": [f"ip route 10.99.0.0/16 {to['R1']['R2']}"]}, topo)
    assert not rep[0]["ok"] and "R1 → R2 → R3 → R1" in rep[0]["detail"], rep
    print("twin ok:", rep[0]["detail"])
