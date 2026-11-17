"""End-to-end on freshly generated labs: generate, deploy, CONNECT, forced rollback, loop blocked, destroy.
Deploys real containers, so it only runs with NMA_LIVE_LABS=1 (about a minute per lab):
    NMA_LIVE_LABS=1 .venv/bin/python -m pytest tests/test_live_labs.py -v
Afterwards, redeploy the lab you want: python agent.py lab deploy triangle-demo"""
import os
import shutil

import pytest

from backend import examples, labctl, labgen
from backend.engine import Engine
from backend.lab import DockerLab
from backend.topology import LABS, Topology

pytestmark = pytest.mark.skipif(os.environ.get("NMA_LIVE_LABS") != "1", reason="set NMA_LIVE_LABS=1 to deploy real labs")
WHO = {"requested_by": "operator1", "reason": "live smoke test"}


@pytest.mark.parametrize("template, n", [("ring", 6), ("tree", 7), ("partial-mesh", 8)])
def test_generated_lab_end_to_end(template, n, tmp_path):
    name = f"smoke-{template}"
    topo = Topology({**labgen.template_spec(name, template, n), "baseline": "none"})
    topo.save()
    try:
        labctl.deploy_lab(topo, log=lambda m: None)
        eng = Engine(DockerLab(topo), topo, tmp_path / "a.db", tmp_path / "s")
        far = max(topo.names, key=lambda r: len(topo.shortest_path(topo.names[0], r)))

        c = eng.plan({"intent": "CONNECT", "source": topo.names[0], "destination": far, **WHO})
        assert c["status"] == "PENDING_APPROVAL", c["reason"]
        done = eng.apply(c["id"], "approver1")
        assert done["status"] == "SUCCESS", done["reason"]

        before = eng.lab.routes()
        c = eng.plan({"intent": "CONNECT", "source": topo.names[1], "destination": far, **WHO})
        failed = eng.apply(c["id"], "approver1", "", "deploy", far)
        assert failed["status"] == "ROLLED_BACK" and eng.lab.routes() == before

        loop = next((e for e in examples.find(eng.lab.routes(), topo) if e["key"] == "loop"), None)
        if loop:
            assert eng.plan(loop["intent"])["stage"] == "twin"
    finally:
        labctl.destroy(name)
        shutil.rmtree(LABS / name, ignore_errors=True)
