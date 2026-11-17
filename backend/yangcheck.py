"""The intent contract as a standard YANG 1.1 module: yang/nma-intent.yang (RFC 7950).

Every request body is checked against it as the RFC 7951 JSON encoding of the input of the matching RPC.
This runs next to the Pydantic schema (backend/schema.py), which also knows the active lab's router names,
so a request has to pass both: the published standard model, and the lab-aware check.
"""
from functools import cache

from yangson import DataModel
from yangson.enumerations import ContentType, ValidationScope
from yangson.exceptions import YangsonException

from backend.topology import ROOT

YANG = ROOT / "yang"


@cache
def _model():
    return DataModel.from_file(str(YANG / "yang-library.json"), [str(YANG)])


def check(doc, rpc="submit-intent"):
    """None if doc is a valid input of the nma-intent RPC, else what is wrong (types, when, must, mandatory)."""
    try:
        _model().from_raw({"nma-intent:input": doc}, subschema=f"nma-intent:{rpc}").validate(ValidationScope.all, ContentType.all)
    except YangsonException as e:
        return str(e).replace("/nma-intent:input", "").replace("{} ", "") or type(e).__name__
    return None
