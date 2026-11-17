"""Stage 3: candidate config from fixed Jinja2 templates, then a command allowlist."""
import re

from jinja2 import Environment, StrictUndefined

_env = Environment(undefined=StrictUndefined)
TEMPLATES = {
    "add": _env.from_string("ip route {{ prefix }} {{ next_hop }}"),
    "remove": _env.from_string("no ip route {{ prefix }} {{ next_hop }}"),
}

_IP = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}"
ALLOWED = re.compile(rf"(?:no )?ip route {_IP}/(?:3[0-2]|[12]?\d) {_IP}")


def generate(ops):
    """ops from checks.semantic -> {router: [config lines]}"""
    candidate = {}
    for router, op, prefix, next_hop in ops:
        candidate.setdefault(router, []).append(TEMPLATES[op].render(prefix=prefix, next_hop=next_hop))
    return candidate


def not_allowed(candidate):
    """Every line must be exactly `ip route <prefix> <ip>` or `no ip route <prefix> <ip>`."""
    return [f"{r}: {line!r}" for r, lines in candidate.items() for line in lines if not ALLOWED.fullmatch(line)]
