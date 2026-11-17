"""Lab generator: topology templates -> lab.yaml spec, and a Topology -> containerlab files + FRR configs.

Generated files go to labs/<name>/build/ (not committed): topology.clab.yml, configs/<node>.conf, vtysh.conf.
Configs are mounted read-only and copied in at boot, so nothing the routers save can change them.
"""
import random

import yaml
from jinja2 import Environment, StrictUndefined

from backend.topology import LABS, MAX_ROUTERS, Topology, TopologyError

IMAGE = "quay.io/frrouting/frr:10.2.1"
_env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
FRR_CONF = _env.from_string("""\
frr defaults traditional
hostname {{ name }}
service integrated-vtysh-config
!
{% for ifname, addrs in interfaces %}
interface {{ ifname }}
{% for a in addrs %}
 ip address {{ a }}
{% endfor %}
exit
!
{% endfor %}
{% for prefix, nh in routes %}
ip route {{ prefix }} {{ nh }}
{% endfor %}
!
end
""")

TEMPLATES = {
    "line": "routers in a row; one path between any two",
    "triangle": "3 routers, every pair linked",
    "ring": "a closed loop; two paths between any two",
    "star": "R1 in the middle, every other router linked only to it",
    "mesh": "every pair linked (up to 8 routers)",
    "tree": "binary tree under R1",
    "partial-mesh": "a ring plus a few random cross links",
}


def template_spec(name, template, n, seed=7):
    if template not in TEMPLATES:
        raise TopologyError(f"unknown template {template!r}, choose one of {', '.join(TEMPLATES)}")
    if template == "triangle":
        n = 3
    if not 1 <= n <= MAX_ROUTERS:
        raise TopologyError(f"router count must be 1-{MAX_ROUTERS}")
    if template == "mesh" and n > 8:
        raise TopologyError("mesh is limited to 8 routers (28 links)")
    r = [f"R{i}" for i in range(1, n + 1)]
    pairs = {
        "line": lambda: list(zip(r, r[1:])),
        "triangle": lambda: [(r[0], r[1]), (r[1], r[2]), (r[0], r[2])],
        "ring": lambda: list(zip(r, r[1:])) + ([(r[-1], r[0])] if n > 2 else []),
        "star": lambda: [(r[0], x) for x in r[1:]],
        "mesh": lambda: [(a, b) for i, a in enumerate(r) for b in r[i + 1:]],
        "tree": lambda: [(r[(i + 1) // 2 - 1], r[i]) for i in range(1, n)],
        "partial-mesh": lambda: _partial(r, seed),
    }[template]()
    return {"name": name, "template": template, "baseline": "shortest-path",
            "routers": r, "links": [list(p) for p in pairs]}


def _partial(r, seed):
    ring = list(zip(r, r[1:])) + ([(r[-1], r[0])] if len(r) > 2 else [])
    have = {frozenset(p) for p in ring}
    rnd, extra = random.Random(seed), []
    candidates = [(a, b) for i, a in enumerate(r) for b in r[i + 2:] if frozenset((a, b)) not in have]
    rnd.shuffle(candidates)
    return ring + candidates[: max(1, len(r) // 3)] if candidates else ring


def frr_conf(topo, router, routes=None):
    routes = topo.baseline_routes() if routes is None else routes
    ifaces = sorted(topo.interfaces(router).items(), key=lambda kv: (kv[0] != "lo", kv[0]))
    return FRR_CONF.render(name=router, interfaces=[(i, [str(a) for a in addrs]) for i, addrs in ifaces],
                           routes=sorted(routes[router]))


def build(topo):
    """Write the containerlab topology and per-router configs. Returns the clab file path."""
    out = LABS / topo.name / "build"
    (out / "configs").mkdir(parents=True, exist_ok=True)
    (out / "vtysh.conf").write_text("service integrated-vtysh-config\n")
    routes = topo.baseline_routes()
    for r in topo.names:
        (out / "configs" / f"{topo.node(r)}.conf").write_text(frr_conf(topo, r, routes))
    clab = {
        "name": topo.name,
        "topology": {
            "kinds": {"linux": {"image": IMAGE,
                                "cmd": 'sh -c "cp /baseline/frr.conf /baseline/vtysh.conf /etc/frr/ && exec /usr/lib/frr/docker-start"'}},
            "nodes": {topo.node(r): {"kind": "linux", "binds": [f"configs/{topo.node(r)}.conf:/baseline/frr.conf:ro",
                                                                 "vtysh.conf:/baseline/vtysh.conf:ro"]} for r in topo.names},
            "links": [{"endpoints": [f"{topo.node(l['a'])}:{l['a_if']}", f"{topo.node(l['b'])}:{l['b_if']}"]}
                      for l in topo.links],
        },
    }
    path = out / "topology.clab.yml"
    path.write_text(f"# generated from labs/{topo.name}/lab.yaml, do not edit\n" + yaml.safe_dump(clab, sort_keys=False))
    return path


def new_lab(name, template, n, seed=7):
    topo = Topology(template_spec(name, template, n, seed))
    topo.save()
    return topo
