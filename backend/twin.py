"""Stage 4: digital twin. A Python model of the 3 routers; the candidate is applied to a copy only.

For every destination, start at every router and follow next-hops (longest prefix match):
  DELIVERED  reached a router where the destination is directly connected
  LOOP       visited the same router twice
  BLACKHOLE  forwarded, then reached a router with no route (or a next-hop that is not a lab router)
  NO_ROUTE   the source itself has no route (plain unreachable, not a fault on its own)
The candidate fails if it creates a new loop, a new blackhole, or breaks reachability that existed before.
"""
from ipaddress import IPv4Network

from backend import net


def _lpm(table, dst):
    best = None
    for prefix, nh in table:
        p = IPv4Network(prefix)
        if dst.subnet_of(p) and (best is None or p.prefixlen > best[0].prefixlen):
            best = (p, nh)
    return best and best[1]  # ponytail: ECMP not modelled, generator never creates it


def walk(routes, src, dst):
    dst, path = IPv4Network(dst), [src]
    while True:
        at = path[-1]
        if net.is_connected(at, dst):
            return "DELIVERED", path
        nh = _lpm(routes.get(at, ()), dst)
        if nh is None:
            return ("NO_ROUTE" if at == src else "BLACKHOLE"), path
        nxt = net.owner(nh)
        if nxt is None:
            return "BLACKHOLE", path
        if nxt in path:
            return "LOOP", path + [nxt]
        path.append(nxt)


def destinations(routes):
    """Every LAN plus every prefix any router has a static route for (so /25 detours are walked too)."""
    ds = {str(a.network) for r in net.NAMES for a in net.lans(r)}
    return ds | {p for table in routes.values() for p, _ in table}


def analyse(routes, dests):
    return {(s, d): walk(routes, s, d) for d in dests for s in net.NAMES if not net.is_connected(s, d)}


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


def validate(before, candidate, removed=None):
    """removed=(router, prefix) for REMOVE_ROUTE: losing that router's own path to it is the point of the change."""
    after = apply_candidate(before, candidate)
    dests = destinations(before) | destinations(after)
    b, a = analyse(before, dests), analyse(after, dests)

    loops = [f"{d} from {s}: {_arrow(p)}" for (s, d), (o, p) in sorted(a.items())
             if o == "LOOP" and b[(s, d)][0] != "LOOP"]
    holes = [f"{d} from {s}: dropped at {p[-1]} ({_arrow(p)})" for (s, d), (o, p) in sorted(a.items())
             if o == "BLACKHOLE" and b[(s, d)][0] != "BLACKHOLE"]
    lost = [f"{s} can no longer reach {d} ({a[(s, d)][0].lower().replace('_', ' ')})"
            for (s, d), (o, _) in sorted(b.items())
            if o == "DELIVERED" and a[(s, d)][0] != "DELIVERED"
            and not (removed and s == removed[0] and IPv4Network(d).subnet_of(IPv4Network(removed[1])))]

    return after, [
        _check("loop", loops, "no new routing loops"),
        _check("blackhole", holes, "no new blackholes"),
        _check("reachability", lost, "every path that worked before still works"),
    ]


if __name__ == "__main__":
    # Self-check: R2 and R3 already point 10.99.0.0/16 around the triangle; R1 closing it is a loop.
    t = {"R1": set(), "R2": {("10.99.0.0/16", "10.0.23.2")}, "R3": {("10.99.0.0/16", "10.0.13.1")}}
    _, rep = validate(t, {"R1": ["ip route 10.99.0.0/16 10.0.12.2"]})
    assert not rep[0]["ok"] and "R1 → R2 → R3 → R1" in rep[0]["detail"], rep
    print("twin ok:", rep[0]["detail"])
