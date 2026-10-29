# AIORI-3 Hackathon: PS-008

**Network Management Agent for Reviewed Configuration Changes**
Category: Cybersecurity

## Team

| Role | Name |
|---|---|
| Team Lead | Mukul Goyal |
| Team Member | Ishika Pandey |
| Faculty Guide | Dr. Tejaswi Khanna |

Team introduction video: https://youtu.be/WGWgj6Aavbs

## What it does

An operator states an intent (`ADD_ROUTE` or `REMOVE_ROUTE`). The agent turns it into FRR commands from fixed templates, proves the change is safe in a digital twin of a 3-router lab (no new loops, no new blackholes, no lost reachability, FRR's own parser accepts it), and waits for a person to approve it. On approval it snapshots all 3 routers to git, deploys, and checks routes and pings. If anything fails, all 3 routers are restored from the snapshot automatically. Every stage is written to a hash-chained SQLite audit log.

```
intent ─▶ schema ─▶ semantic ─▶ template + allowlist ─▶ twin + FRR syntax ─▶ HUMAN APPROVAL
                                                                                 │ approve
        SUCCESS ◀─ health check ◀─ deploy (all-or-nothing) ◀─ snapshot + git ◀───┘
        ROLLED_BACK ◀─ restore all 3 routers ◀─ (any failure)
```

## Project layout

| Path | What it is |
|---|---|
| `lab/topology.clab.yml` | 3 FRR routers (R1, R2, R3) in a triangle, Containerlab |
| `lab/configs/r*/frr.conf` | Baseline router configs (mounted read-only) |
| `lab/inventory.yaml` | Routers, link IPs, LANs. The only place IPs are defined |
| `backend/schema.py` | Stage 1: Pydantic intent schema |
| `backend/checks.py` | Stage 2: semantic checks (connected next-hop, conflict, duplicate, route exists) |
| `backend/generator.py` | Stage 3: Jinja2 templates + command allowlist |
| `backend/twin.py` | Stage 4: digital twin (loop, blackhole, reachability) |
| `backend/lab.py` | The only code that talks to routers (`docker exec … vtysh`), incl. FRR dry-run syntax check |
| `backend/deploy.py` | Stages 7–9: snapshot + git, deploy, health check, rollback, fault injection |
| `backend/audit.py` | SQLite audit log with hash chain |
| `backend/engine.py` | `plan()`, `apply()`, `reject()`: the pipeline, approval timeout |
| `backend/main.py` + `backend/static/index.html` | FastAPI + web dashboard |
| `agent.py` | Terminal front end (demo fallback) |
| `intents/` | Ready-made intents for the demo |
| `tests/` | 20 scenarios; offline by default, `NMA_LIVE=1` runs them on the real routers |
| `docs/` | Intent JSON Schema, test results, mentor pseudocode, slide notes |

## Setup, step by step (macOS, one time)

Containerlab needs Linux, so the routers run in an OrbStack Linux machine called `clab`. The agent runs on the Mac and reaches the routers with `orb -m clab docker …`.

**1. OrbStack and the Linux machine.** Install OrbStack (`brew install orbstack`), open it once, then create the machine:

```bash
orb create ubuntu clab
```

**2. Docker and Containerlab inside the machine.**

```bash
orb -m clab bash -c 'curl -sL https://containerlab.dev/setup | sudo -E bash -s "all"'
```

Log out and in (`orb -m clab`), then check that `docker ps` works without sudo.

**3. Pull the FRR router image.**

```bash
orb -m clab docker pull quay.io/frrouting/frr:10.2.1
```

**4. Start the lab.** OrbStack mounts your Mac home folder in the machine, so the same path works:

```bash
orb -m clab bash -c 'cd ~/Documents/the-lab/arena/AIORI-NMA/lab && sudo containerlab deploy -t topology.clab.yml'
```

**5. Python environment on the Mac.**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

**6. Check that everything works.**

```bash
.venv/bin/python agent.py status
```

```bash
.venv/bin/python -m pytest -q
```

```bash
NMA_LIVE=1 .venv/bin/python -m pytest -v
```

The first test run uses in-memory routers (2 seconds). The second runs the same 20 scenarios on the real FRR routers (about 30 seconds) and resets them to baseline before each test.

## Daily use

Start OrbStack and the lab (step 4; it is a no-op if already running), then pick a front end.

**Dashboard**, at http://localhost:8000:

```bash
.venv/bin/uvicorn backend.main:app --port 8000
```

**Terminal**, which asks you to type `yes` to approve:

```bash
.venv/bin/python agent.py apply intents/1-add-route.json
```

Other commands: `agent.py history`, `agent.py show <id>` (full audit trail), `agent.py reset` (all routers back to baseline), `agent.py verify-audit`, `agent.py schema`.

Stop the lab:

```bash
orb -m clab bash -c 'cd ~/Documents/the-lab/arena/AIORI-NMA/lab && sudo containerlab destroy -t topology.clab.yml'
```

Settings (environment variables): `NMA_APPROVAL_TIMEOUT` (seconds, default 300), `NMA_DB` (default `data/audit.db`), `NMA_DOCKER` (default `orb -m clab docker` on macOS, `docker` elsewhere).

## The lab

| Link / LAN | Addresses |
|---|---|
| R1–R2 | 10.0.12.0/30 (R1 .1, R2 .2) |
| R2–R3 | 10.0.23.0/30 (R2 .1, R3 .2) |
| R1–R3 | 10.0.13.0/30 (R1 .1, R3 .2) |
| LANs | R1 10.10.10.0/24, R2 10.10.20.0/24, R3 10.10.30.0/24 and server LAN 10.10.31.0/24 |

The baseline is set up so every demo story works from a fresh lab: every main LAN reaches every other; R3's server LAN `10.10.31.0/24` is not routed yet (the change to make), and R1 has a legacy detour `10.10.30.128/25 via R2` (the route to clean up, and the bait for the loop).

## Demo script (about 6 minutes)

Run `agent.py reset` first. Use the "Try an example" buttons in the dashboard, or the files in `intents/`.

1. **The lab:** the triangle, the reachability table (R1 and R2 cannot reach `10.10.31.0/24`).
2. **Good change** (`1-add-route.json`, next hop given as `R3`): 4 checks pass, green diff, approve. Watch backup → deploy → 7 LAN pings → SUCCESS. The table now shows R1 reaching `…31.0/24`.
3. **Loop** (`3-loop.json`): blocked; the map draws R2 → R1 → R2 in red.
4. **Blackhole** (`4-blackhole.json`): blocked; traffic would be dropped at R2.
5. **Rejected change:** validate `2-remove-route.json`, click Reject. The routes are untouched.
6. **Failed deploy:** validate `2-remove-route.json` again, set the failure test to "A link goes down after deploy", approve. Pings fail, all 3 routers are restored, status ROLLED_BACK.
7. **History** tab, then `NMA_LIVE=1 .venv/bin/python -m pytest -v`, showing 20 passed.

## Tests (20 scenarios, all passing offline and on the live lab)

| # | Scenario | PS category | Result |
|---|---|---|---|
| 1 | Valid ADD_ROUTE, approved | Happy path | Deployed, health check passes |
| 2 | Valid REMOVE_ROUTE, approved | Happy path | Next-hop looked up, route removed |
| 3 | Missing field | Invalid config | INVALID at schema |
| 4 | Bad IP / prefix with host bits | Invalid config | INVALID at schema |
| 5 | Unknown router R99 | Invalid config | INVALID at schema |
| 6 | Next-hop not connected | Invalid config | BLOCKED at semantic |
| 7 | Same prefix, other next-hop | Conflicting route | BLOCKED (conflict) |
| 8 | Change creates a loop | Conflicting route | BLOCKED by twin, path named (2- and 3-router loops) |
| 9 | Human rejects | Rejected approval | Routers untouched; can never deploy later |
| 10 | Approval timeout | Rejected approval | Treated as reject |
| 11 | R3 fails mid-deploy | Failed deployment | All 3 routers rolled back |
| 12 | Deploy OK, pings fail | Failed deployment | Automatic rollback, network restored |
| 13 | Command injection (`; reboot`, newline, extra field) | Invalid config | INVALID; allowlist rejects non-route lines |
| 14 | Duplicate route | Conflicting route | BLOCKED as no-op |
| 15 | Remove a route that does not exist | Invalid config | BLOCKED |
| 16 | Invalid intent type | Invalid config | INVALID |
| 17 | Change creates a blackhole | Conflicting route | BLOCKED by twin |
| 18 | Several changes in a row | Happy path | Each applied, snapshotted and logged separately |
| 19 | Rollback verification | Failed deployment | Running-config equals snapshot byte for byte |
| 20 | Audit log | Auditability | Every stage recorded; tampering breaks the hash chain |

Saved output: `docs/test-results.txt`.

## Security principles

| Principle | Where |
|---|---|
| Least privilege | Only 2 intent types (`schema.py`); routers outside the inventory are rejected |
| No arbitrary commands | Templates + strict allowlist regex (`generator.py`); the operator never types CLI |
| Input validation | Pydantic with `extra="forbid"`, typed IPs, safe-text patterns (`schema.py`), semantic checks (`checks.py`) |
| Test before change | Digital twin + FRR `vtysh --dryrun` before approval (`twin.py`, `lab.py`) |
| Human-in-the-loop | Approve/reject with name and reason; timeout = reject (`engine.py`) |
| Safe recovery | Git-committed snapshots, all-router rollback, re-check after rollback (`deploy.py`) |
| Auditability | Every stage logged with who, what, when, why; SHA-256 hash chain (`audit.py`) |
| Isolation | Lab only; next-hops must be lab routers, so the management network cannot be used |
| Consistency | Apply re-reads the routes and refuses if the network changed since validation (`engine.py`) |

## Choices that differ from the original plan

- **Dashboard** is one HTML file served by FastAPI instead of Next.js: no build step and one process, so there is no need for Docker Compose either. Each of the 5 screens is there: network status, create change, validation result, approval with diff, history.
- **Twin** walks next-hops in plain Python instead of networkx; the walk takes about 15 lines.
- **`/validate`** is folded into `POST /api/intent`, which validates and returns the report in one call.
- **LLM front door** is not built (optional in the PS).
