"""Stage 2: semantic checks. Plain route tables in, so they run without routers."""
from backend import net
from backend.schema import AddRoute


def semantic(intent, routes):
    """Returns (error or None, next_hops the generator should use)."""
    router, dest = intent.router, str(intent.destination)
    if net.is_connected(router, dest):
        return f"{dest} is directly connected on {router}, a static route is not needed", []
    existing = sorted(nh for p, nh in routes[router] if p == dest)

    if isinstance(intent, AddRoute):
        nh, nbrs = str(intent.next_hop), net.neighbours(router)
        if nh not in nbrs:
            options = ", ".join(f"{ip} ({n})" for ip, n in nbrs.items())
            return f"next-hop {nh} is not a directly connected neighbour of {router} (allowed: {options})", []
        if nh in existing:
            return f"duplicate: {router} already has ip route {dest} {nh}, nothing to do", []
        if existing:
            return f"conflict: {router} already routes {dest} via {', '.join(existing)}; remove that route first", []
        return None, [nh]

    if not existing:
        return f"{router} has no static route for {dest}, nothing to remove", []
    return None, existing
