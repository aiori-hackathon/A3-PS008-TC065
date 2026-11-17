"""Stage 1: structured intent schema (Pydantic). Router names are checked against the active lab,
passed in as validation context: parse(raw, topo). Exports JSON Schema for the report and the LLM prompt.

  ADD_ROUTE     one static route on one router
  REMOVE_ROUTE  remove a static route (the agent looks up its next hop)
  CONNECT       "R1 should reach R5": the agent computes the path and adds routes on every router along it
"""
from ipaddress import IPv4Address, IPv4Network
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, ValidationInfo, field_validator

# Free text never reaches a router, but still: no shell or CLI metacharacters (; | & $ ` < > newline).
SAFE_TEXT = r"^[A-Za-z0-9 _.,:/()'?!-]{1,200}$"
SAFE_NAME = r"^[A-Za-z0-9_.-]{1,32}$"


def _topo(info: ValidationInfo):
    topo = (info.context or {}).get("topo")
    if topo is None:
        raise ValueError("no lab is loaded")
    return topo


def _router(v, info):
    names = {n.lower(): n for n in _topo(info).names}
    if not isinstance(v, str) or v.lower() not in names:
        raise ValueError(f"unknown router {v!r}, this lab has {', '.join(names.values())}")
    return names[v.lower()]


class _Common(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields such as "command" are rejected
    requested_by: str = Field(pattern=SAFE_NAME)
    reason: str = Field(pattern=SAFE_TEXT)


class _Routed(_Common):
    router: str = Field(description="Target router, e.g. R1")
    destination: IPv4Network = Field(description="Destination prefix in CIDR, e.g. 10.10.30.0/24")

    @field_validator("router", mode="before")
    @classmethod
    def known_router(cls, v, info: ValidationInfo):
        return _router(v, info)


class AddRoute(_Routed):
    intent: Literal["ADD_ROUTE"]
    next_hop: IPv4Address = Field(description="Next-hop IP, or a neighbouring router name (converted to its link IP)")

    @field_validator("next_hop", mode="before")
    @classmethod
    def router_name_to_ip(cls, v, info: ValidationInfo):
        topo = _topo(info)
        names = {n.lower(): n for n in topo.names}
        if isinstance(v, str) and v.lower() in names:
            router = info.data.get("router")
            ip = router and topo.neighbour_ip(router, names[v.lower()])
            if not ip:
                raise ValueError(f"{v} is not a direct neighbour of {router}")
            return ip
        return v


class RemoveRoute(_Routed):
    intent: Literal["REMOVE_ROUTE"]


class Connect(_Common):
    intent: Literal["CONNECT"]
    source: str = Field(description="Router that must be able to reach the destination, e.g. R1")
    destination: str = Field(description="A router name (all of its LANs) or a prefix inside the lab, e.g. R5 or 10.10.5.0/24")
    both_ways: bool = Field(True, description="Also make the destination reach the source's LANs (needed for ping)")

    @field_validator("source", mode="before")
    @classmethod
    def known_source(cls, v, info: ValidationInfo):
        return _router(v, info)

    @field_validator("destination", mode="before")
    @classmethod
    def router_or_prefix(cls, v, info: ValidationInfo):
        names = {n.lower(): n for n in _topo(info).names}
        if isinstance(v, str) and v.lower() in names:
            return names[v.lower()]
        try:
            return str(IPv4Network(v))
        except (ValueError, TypeError):
            raise ValueError(f"{v!r} is neither a router of this lab nor an IPv4 prefix")


Intent = Annotated[Union[AddRoute, RemoveRoute, Connect], Field(discriminator="intent")]
_adapter = TypeAdapter(Intent)


def parse(raw, topo):
    return _adapter.validate_python(raw, context={"topo": topo})


def errors(exc: ValidationError):
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'intent'}: {e['msg']}" for e in exc.errors())


def json_schema():
    return _adapter.json_schema()
