"""Stage 3: candidate config from fixed Jinja2 templates, then a command allowlist."""
import re

from jinja2 import Environment, StrictUndefined

_env = Environment(undefined=StrictUndefined, trim_blocks=True)
TEMPLATES = {
    "ADD_ROUTE": _env.from_string("{% for nh in next_hops %}ip route {{ destination }} {{ nh }}\n{% endfor %}"),
    "REMOVE_ROUTE": _env.from_string("{% for nh in next_hops %}no ip route {{ destination }} {{ nh }}\n{% endfor %}"),
}

_IP = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}"
ALLOWED = re.compile(rf"(?:no )?ip route {_IP}/(?:3[0-2]|[12]?\d) {_IP}")


def generate(intent, next_hops):
    text = TEMPLATES[intent.intent].render(destination=intent.destination, next_hops=next_hops)
    return {intent.router: text.splitlines()}


def not_allowed(candidate):
    """Every line must be exactly `ip route <prefix> <ip>` or `no ip route <prefix> <ip>`."""
    return [f"{r}: {line!r}" for r, lines in candidate.items() for line in lines if not ALLOWED.fullmatch(line)]
