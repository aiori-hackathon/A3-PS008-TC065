"""Demo examples for whatever lab is active, found with the twin instead of hard-coded.
Each example is a ready intent plus the outcome it should produce, so the dashboard buttons work on any topology."""
from backend import checks, generator, twin
from backend.schema import parse
from backend.topology import net_of

WHO = {"requested_by": "operator1"}
TEST_NET = "192.0.2.0/24"  # RFC 5737 documentation range: never part of a lab


def _outcome(intent, routes, topo):
    """'ok' or the stage that stops it: 'semantic', 'loop', 'blackhole', 'reachability'."""
    parsed = parse(intent, topo)
    error, ops = checks.semantic(parsed, routes, topo)
    if error:
        return "semantic"
    removed = (parsed.router, str(parsed.destination)) if parsed.intent == "REMOVE_ROUTE" else None
    _, report = twin.validate(routes, generator.generate(ops), topo, removed)
    return next((r["check"] for r in report if not r["ok"]), "ok")


def _halves(prefix):
    return [str(s) for s in net_of(prefix).subnets(prefixlen_diff=1)] if net_of(prefix).prefixlen < 32 else []


def find(routes, topo, budget=60):
    found, tries = {}, [0]
    tables = twin._prepare(routes)

    def attempt(key, intent, want):
        if key in found or tries[0] >= budget:
            return
        tries[0] += 1
        if _outcome(intent, routes, topo) == want:
            found[key] = intent

    lans = topo.all_lans()
    # CONNECT: a pair of routers that cannot reach each other yet
    for s in topo.names:
        for lan, owner in lans.items():
            if owner != s and twin.walk(routes, s, lan, topo, tables)[0] != "DELIVERED":
                found.setdefault("connect", {"intent": "CONNECT", "source": s, "destination": owner,
                                             "reason": f"{s} users need to reach {owner}", **WHO})
    # safe ADD: route a LAN (or half of it) via a neighbour that delivers without coming back
    for r in topo.names:
        unreached_first = sorted(lans.items(), key=lambda kv: twin.walk(routes, r, kv[0], topo, tables)[0] == "DELIVERED")
        for lan, owner in unreached_first:
            if owner == r:
                continue
            for nh, n in topo.neighbours(r).items():
                state, path = twin.walk(routes, n, lan, topo, tables)
                if state == "DELIVERED" and r not in path:
                    for prefix in [lan] + _halves(lan):
                        if not any(p == prefix for p, _ in routes[r]):
                            attempt("add", {"intent": "ADD_ROUTE", "router": r, "destination": prefix, "next_hop": n,
                                            "reason": f"send {prefix} from {r} via {n}", **WHO}, "ok")
                            break
    # removable route
    for r in topo.names:
        for prefix, _ in sorted(routes[r], key=lambda x: -net_of(x[0]).prefixlen):
            attempt("remove", {"intent": "REMOVE_ROUTE", "router": r, "destination": prefix,
                               "reason": f"{prefix} route on {r} no longer needed", **WHO}, "ok")
    # loop: router n sends part of a remote LAN back to the router r that forwards it to n
    for r in topo.names:
        for prefix, nh in sorted(routes[r]):
            n = topo.owner(nh)
            if n and not topo.is_connected(n, prefix):
                for half in _halves(prefix):
                    attempt("loop", {"intent": "ADD_ROUTE", "router": n, "destination": half, "next_hop": r,
                                     "reason": f"send {half} from {n} back via {r}", **WHO}, "loop")
    # blackhole: a network nobody has, handed to a neighbour with no route for it
    for r in topo.names:
        for n in topo.neighbours(r).values():
            attempt("blackhole", {"intent": "ADD_ROUTE", "router": r, "destination": TEST_NET, "next_hop": n,
                                  "reason": f"route a network nobody has via {n}", **WHO}, "blackhole")
    # conflict: same prefix as an existing route, different neighbour
    for r in topo.names:
        for prefix, nh in sorted(routes[r]):
            other = next((n for ip, n in topo.neighbours(r).items() if ip != nh), None)
            if other:
                found.setdefault("conflict", {"intent": "ADD_ROUTE", "router": r, "destination": prefix, "next_hop": other,
                                              "reason": f"reach {prefix} via {other} instead", **WHO})
    r0 = topo.names[0]
    found["injection"] = {"intent": "ADD_ROUTE", "router": r0, "destination": f"{TEST_NET}; reboot",
                          "next_hop": next(iter(topo.neighbours(r0).values()), r0), "reason": "testing", **WHO}

    labels = {"add": ("Add a route", "passes every check"), "remove": ("Remove a route", "passes every check"),
              "connect": ("Connect two routers", "routes added along the path"),
              "loop": ("Create a loop", "blocked by the twin"), "blackhole": ("Create a blackhole", "blocked by the twin"),
              "conflict": ("Conflicting route", "blocked as a conflict"), "injection": ("Command injection", "rejected by the schema")}
    return [{"key": k, "label": labels[k][0], "expect": labels[k][1], "intent": found[k]} for k in labels if k in found]
