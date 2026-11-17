"""Stage 2: semantic checks. Plain route tables in, so they run without routers.

semantic() returns (error or None, ops), where ops are (router, "add" | "remove", prefix, next_hop)
for the generator to render. CONNECT is compiled here: shortest path, then a route on each hop
that does not already deliver.
"""
from backend import twin
from backend.schema import AddRoute, Connect


def semantic(intent, routes, topo):
    if isinstance(intent, Connect):
        return _connect(intent, routes, topo)
    router, dest = intent.router, str(intent.destination)
    if topo.is_connected(router, dest):
        return f"{dest} is directly connected on {router}, a static route is not needed", []
    existing = sorted(nh for p, nh in routes[router] if p == dest)

    if isinstance(intent, AddRoute):
        nh, nbrs = str(intent.next_hop), topo.neighbours(router)
        if nh not in nbrs:
            options = ", ".join(f"{ip} ({n})" for ip, n in nbrs.items()) or "none, it has no links"
            return f"next-hop {nh} is not a directly connected neighbour of {router} (allowed: {options})", []
        if nh in existing:
            return f"duplicate: {router} already has ip route {dest} {nh}, nothing to do", []
        if existing:
            return f"conflict: {router} already routes {dest} via {', '.join(existing)}; remove that route first", []
        return None, [(router, "add", dest, nh)]

    if not existing:
        return f"{router} has no static route for {dest}, nothing to remove", []
    return None, [(router, "remove", dest, nh) for nh in existing]


def _connect(intent, routes, topo):
    src = intent.source
    if intent.destination in topo.names:
        owner, prefixes = intent.destination, [str(a.network) for a in topo.lans(intent.destination)]
    else:
        owner = next((r for r in topo.names if topo.is_connected(r, intent.destination)), None)
        prefixes = [intent.destination]
        if not owner:
            return f"no router in this lab has {intent.destination}; CONNECT can only reach networks inside the lab", []
    if owner == src:
        return f"{intent.destination} is already on {src}", []

    goals = [(src, p, owner) for p in prefixes]
    if intent.both_ways:
        goals += [(owner, str(a.network), src) for a in topo.lans(src)]
    table = {r: set(t) for r, t in routes.items()}
    ops = []
    for start, prefix, target in goals:
        path = topo.shortest_path(start, target)
        if not path:
            return f"{start} and {target} are not linked in this lab", []
        for hop, nxt in zip(path, path[1:]):
            if twin.walk(table, hop, prefix, topo)[0] == "DELIVERED":
                break  # the rest of the path already works
            existing = sorted(n for p, n in table[hop] if p == prefix)
            if existing:
                return (f"conflict: {hop} already routes {prefix} via {', '.join(existing)} and that path does not "
                        f"deliver; remove that route first"), []
            nh = topo.neighbour_ip(hop, nxt)
            table[hop].add((prefix, nh))
            ops.append((hop, "add", prefix, nh))
    if not ops:
        return f"{src} and {intent.destination} can already reach each other, nothing to do", []
    return None, ops
