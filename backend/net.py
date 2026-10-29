"""Inventory and route-table helpers. Everything about the lab comes from lab/inventory.yaml.

A route table is {router: {(prefix, next_hop), ...}} with both values as strings,
e.g. {"R1": {("10.10.20.0/24", "10.0.12.2")}}.
"""
import re
from ipaddress import IPv4Interface, IPv4Network
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
INV = yaml.safe_load((ROOT / "lab" / "inventory.yaml").read_text())
ROUTERS = INV["routers"]
NAMES = list(ROUTERS)


def interfaces(router):
    """{ifname: [IPv4Interface, ...]} for one router."""
    return {name: [IPv4Interface(a) for a in (v if isinstance(v, list) else [v])]
            for name, v in ROUTERS[router]["interfaces"].items()}


def addresses(router):
    return [a for addrs in interfaces(router).values() for a in addrs]


def connected(router):
    return [a.network for a in addresses(router)]


def is_connected(router, prefix):
    return any(IPv4Network(prefix).subnet_of(c) for c in connected(router))


def lans(router):
    """LAN addresses live on lo."""
    return interfaces(router).get("lo", [])


def lan_net(router):
    """The LAN that holds the router's ping source address."""
    return next(a.network for a in lans(router) if str(a.ip) == ROUTERS[router]["lan_ip"])


def owner(ip):
    """Router that owns interface address `ip`, or None."""
    return next((r for r in NAMES for a in addresses(r) if str(a.ip) == str(ip)), None)


def neighbours(router):
    """{neighbour_ip: neighbour_name} for every router sharing a link subnet with `router`."""
    out = {}
    for mine in addresses(router):
        for other in NAMES:
            if other != router:
                out.update({str(a.ip): other for a in addresses(other) if a.network == mine.network})
    return out


def neighbour_ip(router, neighbour):
    return next((ip for ip, n in neighbours(router).items() if n == neighbour), None)


ROUTE_LINE = re.compile(r"^ip route (\S+) (\S+)$")


def parse_static(config):
    """Static routes from FRR running-config text."""
    return {m.groups() for m in map(ROUTE_LINE.match, config.splitlines()) if m}


def to_json(routes):
    return {r: sorted(map(list, t)) for r, t in routes.items()}


def from_json(data):
    return {r: {tuple(x) for x in t} for r, t in data.items()}
