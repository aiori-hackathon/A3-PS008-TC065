"""Lab manager: which lab is active, and deploying / destroying labs with containerlab in the VM.

One lab runs at a time: deploying a lab destroys any other lab this project manages.
Deploys run in a background thread so the dashboard can show progress (JOBS).
"""
import json
import threading
import time

from backend import deploy, labgen
from backend.lab import VM, DockerLab, LabError, run
from backend.topology import ROOT, Topology, lab_names

ACTIVE = ROOT / "data" / "active_lab"
DEFAULT = "triangle-demo"
CLAB = [*VM, "sudo", "-n", "containerlab"]
JOBS = {}  # lab name -> {"state": running|done|failed, "log": [...]}
_lock = threading.Lock()


def active_name():
    try:
        name = ACTIVE.read_text().strip()
    except OSError:
        return DEFAULT
    return name if name in lab_names() else DEFAULT


def active_topology():
    return Topology.load(active_name())


def set_active(name):
    Topology.load(name)  # must exist and be valid
    ACTIVE.parent.mkdir(exist_ok=True)
    ACTIVE.write_text(name + "\n")


def running():
    """{lab name: [container states]} for labs containerlab knows about."""
    p = run([*CLAB, "inspect", "--all", "--format", "json"], timeout=60)
    if p.returncode != 0:
        if "no containers found" in (p.stdout + p.stderr).lower():
            return {}
        raise LabError(f"containerlab inspect failed: {(p.stdout + p.stderr).strip()[-300:]}")
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return {lab: [c["state"] for c in nodes] for lab, nodes in data.items()}


def destroy(name):
    path = ROOT / "labs" / name / "build" / "topology.clab.yml"
    if not path.exists():
        labgen.build(Topology.load(name))
    p = run([*CLAB, "destroy", "-t", str(path), "--cleanup"], timeout=300)
    if p.returncode != 0:
        raise LabError(f"destroy {name} failed: {(p.stdout + p.stderr).strip()[-300:]}")


def deploy_lab(topo, log=print, ready_timeout=90):
    """Generate files, stop other managed labs, deploy, wait until every interface is up and every link passes traffic."""
    log(f"generating containerlab files and {len(topo.names)} router configs")
    path = labgen.build(topo)
    for other in set(running()) & set(lab_names()) - {topo.name}:
        log(f"stopping lab {other} (one lab runs at a time)")
        destroy(other)
    log(f"starting {len(topo.names)} FRR routers and {len(topo.links)} links")
    p = run([*CLAB, "deploy", "-t", str(path), "--reconfigure"], timeout=900)
    if p.returncode != 0:
        raise LabError(f"containerlab deploy failed: {(p.stdout + p.stderr).strip()[-500:]}")
    log("waiting for interfaces and links to come up")
    lab, deadline, detail = DockerLab(topo), time.time() + ready_timeout, ""
    while time.time() < deadline:
        try:
            ok, detail = deploy.preflight(lab, topo)
        except LabError as e:
            ok, detail = False, str(e)
        if ok:
            (path.parent / "deployed_at").write_text(f"{time.time():.3f}\n")  # a fresh deploy resets the approved state
            set_active(topo.name)
            log(f"ready: {detail}")
            return detail
        time.sleep(2)
    raise LabError(f"lab started but is not healthy after {ready_timeout}s: {detail}")


def start_deploy(name):
    """Background deploy for the dashboard. Returns the job dict."""
    topo = Topology.load(name)
    with _lock:
        if any(j["state"] == "running" for j in JOBS.values()):
            raise ValueError("another lab deploy is already running")
        job = JOBS[name] = {"state": "running", "log": [], "started": time.time()}

    def work():
        try:
            deploy_lab(topo, log=job["log"].append)
            job["state"] = "done"
        except Exception as e:  # report any failure to the dashboard instead of dying silently
            job["log"].append(f"failed: {e}")
            job["state"] = "failed"

    threading.Thread(target=work, daemon=True).start()
    return job
