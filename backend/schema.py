"""Stage 1: structured intent schema (Pydantic). Exports JSON Schema for the report."""
from ipaddress import IPv4Address, IPv4Network
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, ValidationInfo, field_validator

from backend import net

# Free text never reaches a router, but still: no shell or CLI metacharacters (; | & $ ` < > newline).
SAFE_TEXT = r"^[A-Za-z0-9 _.,:/()'?!-]{1,200}$"
SAFE_NAME = r"^[A-Za-z0-9_.-]{1,32}$"


class _Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields such as "command" are rejected

    router: str = Field(description="Target router, e.g. R1")
    destination: IPv4Network = Field(description="Destination prefix in CIDR, e.g. 10.10.30.0/24")
    requested_by: str = Field(pattern=SAFE_NAME)
    reason: str = Field(pattern=SAFE_TEXT)

    @field_validator("router")
    @classmethod
    def known_router(cls, v):
        if v not in net.NAMES:
            raise ValueError(f"unknown router {v!r}, must be one of {', '.join(net.NAMES)}")
        return v


class AddRoute(_Intent):
    intent: Literal["ADD_ROUTE"]
    next_hop: IPv4Address = Field(description="Next-hop IP, or a neighbouring router name (converted to its link IP)")

    @field_validator("next_hop", mode="before")
    @classmethod
    def router_name_to_ip(cls, v, info: ValidationInfo):
        if isinstance(v, str) and v.upper() in net.ROUTERS:
            router = info.data.get("router")
            ip = router and net.neighbour_ip(router, v.upper())
            if not ip:
                raise ValueError(f"{v} is not a direct neighbour of {router}")
            return ip
        return v


class RemoveRoute(_Intent):
    intent: Literal["REMOVE_ROUTE"]


Intent = Annotated[Union[AddRoute, RemoveRoute], Field(discriminator="intent")]
_adapter = TypeAdapter(Intent)


def parse(raw):
    return _adapter.validate_python(raw)


def errors(exc: ValidationError):
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'intent'}: {e['msg']}" for e in exc.errors())


def json_schema():
    return _adapter.json_schema()
