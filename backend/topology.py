"""A lab is data: labs/<name>/lab.yaml. Everything else (addresses, interfaces, baseline routes,
containerlab files, the dashboard map) is derived from it here.

Minimal lab.yaml:
    name: ring6
    routers: [R1, R2, R3, R4, R5, R6]
    links: [[R1, R2], [R2, R3], [R3, R4], [R4, R5], [R5, R6], [R6, R1]]

Optional, per router:   R3: {lans: [10.10.30.1/24, 10.10.31.1/24]}   (default: one LAN 10.10.<n>.1/24)
Optional, per link:     {a: R1, b: R2, subnet: 10.0.12.0/30}           (default: next free /30 in 10.0.0.0/16)
Optional:               baseline: shortest-path | none                 (default shortest-path)
                        routes: {R1: [[10.10.30.128/25, R2]]}           extra static routes (next hop: IP or neighbour)
                        layout: {R1: [x, y]}                            map positions saved by the dashboard
                        template: ring                                  how it was generated (drives the map layout)

A route table is {router: {(prefix, next_hop), ...}} with string values.
"""
import re
from collections import deque
from functools import lru_cache
from ipaddress import IPv4Interface, IPv4Network
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LABS = ROOT / "labs"
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
LAB_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
MAX_ROUTERS = 30  # ponytail: the OrbStack VM has 4 GB; FRR is ~30 MB per router


class TopologyError(ValueError):
    pass


@lru_cache(maxsize=None)
def net_of(prefix):
    return IPv4Network(prefix)


class Topology:
    def __init__(self, spec):
        self.spec = spec
        self.name = spec.get("name", "")
        if not LAB_NAME.match(self.name):
            raise TopologyError(f"lab name {self.name!r} must be lowercase letters, digits and dashes")
        routers = spec.get("routers") or []
        routers = {r: {} for r in routers} if isinstance(routers, list) else {r: (v or {}) for r, v in routers.items()}
        if not routers:
            raise TopologyError("a lab needs at least one router")
        if len(routers) > MAX_ROUTERS:
            raise TopologyError(f"at most {MAX_ROUTERS} routers per lab")
        for r in routers:
            if not NAME.match(str(r)):
                raise TopologyError(f"router name {r!r} must start with a letter (letters, digits, - and _)")
        nodes = [str(r).lower() for r in routers]
        if len(set(nodes)) != len(nodes):
            raise TopologyError("router names must be unique ignoring case")
        self.names = list(routers)
        self.template = spec.get("template", "custom")
        self.layout = {r: list(p) for r, p in (spec.get("layout") or {}).items() if r in routers}

        # interfaces: lo carries the LANs; links get eth1, eth2, ... in link order
        self._ifaces = {r: {"lo": [IPv4Interface(a) for a in (v.get("lans") or [f"10.10.{i}.1/24"])]}
                        for i, (r, v) in enumerate(routers.items(), start=1)}
        self.links = []
        pool = (IPv4Network(f"10.0.{k // 64}.{(k % 64) * 4}/30") for k in range(64 * 256))
        used = set()
        for raw in spec.get("links") or []:
            a, b, subnet = (raw["a"], raw["b"], raw.get("subnet")) if isinstance(raw, dict) else (raw[0], raw[1], None)
            if a not in routers or b not in routers or a == b:
                raise TopologyError(f"link {a}–{b} must join two different routers of this lab")
            if any({a, b} == {l["a"], l["b"]} for l in self.links):
                raise TopologyError(f"duplicate link {a}–{b}")
            net = IPv4Network(subnet) if subnet else next(n for n in pool if n not in used)
            used.add(net)
            hosts = list(net.hosts())
            a_if, b_if = f"eth{len(self._ifaces[a])}", f"eth{len(self._ifaces[b])}"
            self._ifaces[a][a_if] = [IPv4Interface(f"{hosts[0]}/{net.prefixlen}")]
            self._ifaces[b][b_if] = [IPv4Interface(f"{hosts[1]}/{net.prefixlen}")]
            self.links.append({"a": a, "a_if": a_if, "a_ip": str(hosts[0]), "b": b, "b_if": b_if,
                               "b_ip": str(hosts[1]), "subnet": str(net)})

        nets = [(r, a.network) for r in self.names for addrs in self._ifaces[r].values() for a in addrs]
        for i, (r1, n1) in enumerate(nets):
            for r2, n2 in nets[i + 1:]:
                if n1.overlaps(n2) and not (n1 == n2 and r1 != r2 and any(l["subnet"] == str(n1) for l in self.links)):
                    raise TopologyError(f"address overlap: {n1} on {r1} and {n2} on {r2}")

        # lookups computed once (the twin calls these thousands of times)
        self._owner = {str(a.ip): r for r in self.names for addrs in self._ifaces[r].values() for a in addrs}
        self._connected = {r: [a.network for addrs in self._ifaces[r].values() for a in addrs] for r in self.names}
        self._nbrs = {r: {} for r in self.names}
        for l in self.links:
            self._nbrs[l["a"]][l["b_ip"]] = l["b"]
            self._nbrs[l["b"]][l["a_ip"]] = l["a"]
        self._conn_cache = {}

    # ---------- loading ----------
    @classmethod
    def load(cls, name):
        path = LABS / name / "lab.yaml"
        if not path.exists():
            raise TopologyError(f"no lab named {name!r} (looked for {path})")
        return cls(yaml.safe_load(path.read_text()))

    def save(self):
        folder = LABS / self.name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "lab.yaml").write_text(yaml.safe_dump(self.spec, sort_keys=False, default_flow_style=None))

    # ---------- lookups ----------
    def node(self, router):
        return router.lower()

    def container(self, router):
        return f"clab-{self.name}-{self.node(router)}"

    def interfaces(self, router):
        return self._ifaces[router]

    def addresses(self, router):
        return [a for addrs in self._ifaces[router].values() for a in addrs]

    def connected(self, router):
        return self._connected[router]

    def is_connected(self, router, prefix):
        key = (router, str(prefix))
        if key not in self._conn_cache:
            p = net_of(str(prefix))
            self._conn_cache[key] = any(p.subnet_of(c) for c in self._connected[router])
        return self._conn_cache[key]

    def lans(self, router):
        return self._ifaces[router]["lo"]

    def lan_ip(self, router):
        return str(self.lans(router)[0].ip)

    def lan_net(self, router):
        return self.lans(router)[0].network

    def all_lans(self):
        return {str(a.network): r for r in self.names for a in self.lans(r)}

    def owner(self, ip):
        return self._owner.get(str(ip))

    def neighbours(self, router):
        return self._nbrs[router]

    def neighbour_ip(self, router, neighbour):
        return next((ip for ip, n in self._nbrs[router].items() if n == neighbour), None)

    def shortest_path(self, src, dst):
        """Router names from src to dst over links (fewest hops), or None."""
        prev, q = {src: None}, deque([src])
        while q:
            u = q.popleft()
            if u == dst:
                path = []
                while u:
                    path.append(u)
                    u = prev[u]
                return path[::-1]
            for v in sorted(self._nbrs[u].values(), key=self.names.index):
                if v not in prev:
                    prev[v] = u
                    q.append(v)
        return None

    def resolve_next_hop(self, router, value):
        """A neighbour's name or an IP -> IP string."""
        if value in self.names:
            ip = self.neighbour_ip(router, value)
            if not ip:
                raise TopologyError(f"{value} is not a direct neighbour of {router}")
            return ip
        return str(value)

    # ---------- baseline ----------
    def baseline_routes(self):
        routes = {r: set() for r in self.names}
        if self.spec.get("baseline", "shortest-path") == "shortest-path":
            for dst in self.names:
                for src in self.names:
                    path = self.shortest_path(src, dst) if src != dst else None
                    if path and len(path) > 1:
                        nh = self.neighbour_ip(src, path[1])
                        routes[src] |= {(str(lan.network), nh) for lan in self.lans(dst)}
        for r, extra in (self.spec.get("routes") or {}).items():
            if r not in routes:
                raise TopologyError(f"routes given for unknown router {r}")
            for prefix, nh in extra:
                routes[r].add((str(IPv4Network(prefix)), self.resolve_next_hop(r, nh)))
        return routes

    def summary(self):
        """What the dashboard and the LLM prompt need to know about this lab."""
        return {"name": self.name, "template": self.template, "layout": self.layout,
                "routers": {r: {"node": self.node(r), "lans": [str(a) for a in self.lans(r)],
                                "interfaces": {i: [str(a) for a in addrs] for i, addrs in self._ifaces[r].items()},
                                "neighbours": self._nbrs[r]} for r in self.names},
                "links": self.links}


def lab_names():
    return sorted(p.parent.name for p in LABS.glob("*/lab.yaml"))


def parse_static(config):
    """Static routes from FRR running-config text."""
    return {m.groups() for m in map(ROUTE_LINE.match, config.splitlines()) if m}


ROUTE_LINE = re.compile(r"^ip route (\S+) (\S+)$")


def to_json(routes):
    return {r: sorted(map(list, t)) for r, t in routes.items()}


def from_json(data):
    return {r: {tuple(x) for x in t} for r, t in data.items()}
