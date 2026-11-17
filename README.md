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
Working Demo Video Link: https://youtu.be/j9w14NcGQWg

## What it does

An intent-based network agent for a lab of FRR routers. You design any topology (from a template or by drawing it), and the agent:
1. Generates the lab, deploys it, and draws its map by itself.
2. Takes requests as a form, as JSON, or in plain language ("connect R1 to R4", "R1 ko R4 tak pahunchao").
3. Compiles them into router commands from fixed templates.
4. Proves each change is safe in a digital twin.
5. Waits for a second person to approve it.
6. Deploys it to all routers as one transaction, verifies it, and rolls back every router automatically if anything fails.

Every step lands in a tamper-evident audit log.

```
 plain language ─▶ grammar / LLM ─┐
 form / JSON / CLI ───────────────┴▶ schema ─▶ pre-flight ─▶ drift check ─▶ semantic (CONNECT path compiler)
                                         ─▶ templates + allowlist ─▶ twin (loop, blackhole, reachability) + FRR syntax
                                         ─▶ APPROVAL by someone else ─▶ pre-flight ─▶ snapshot + git ─▶ deploy all routers
                                         ─▶ health (links, routes, LAN pings) ─▶ SUCCESS  |  restore all routers ─▶ ROLLED_BACK
```

Only `ip route …` / `no ip route …` ever reach a router. The language model, if used, only proposes intent JSON, which goes through every check above.

## Project layout

| Path | What it is |
|---|---|
| `labs/<name>/lab.yaml` | A lab as data: routers, links, optional addresses/routes/layout. Everything else is generated |
| `labs/<name>/build/` | Generated containerlab file and router configs (not committed) |
| `backend/topology.py` | Loads a lab: automatic addressing, validation, fast lookups, shortest paths |
| `backend/labgen.py` | Templates (line, triangle, ring, star, mesh, tree, partial mesh) and config generation |
| `backend/labctl.py` | Deploy / stop labs with containerlab in the VM, readiness wait, active lab |
| `backend/schema.py` | Stage 1: intents `ADD_ROUTE`, `REMOVE_ROUTE`, `CONNECT` (Pydantic, checked against the active lab) |
| `backend/checks.py` | Stage 2: semantic checks and the CONNECT path compiler |
| `backend/generator.py` | Stage 3: Jinja2 templates + strict command allowlist |
| `backend/twin.py` | Stage 4: digital twin (loop, blackhole, reachability) |
| `backend/lab.py` | The only code that talks to routers (`docker exec … vtysh`, parallel), FRR dry-run syntax check |
| `backend/deploy.py` | Pre-flight, snapshot + git, deploy, health check, rollback, fault injection |
| `backend/engine.py` | The pipeline, approval timeout, separation of duties, drift detection |
| `backend/nlp.py` | Plain language → intents (offline grammar, else any OpenAI-compatible LLM) |
| `backend/auth.py` | Users, roles, scrypt passwords, sessions |
| `backend/audit.py` | SQLite audit log with a SHA-256 hash chain |
| `backend/examples.py` | Demo examples found by the twin for whatever lab is active |
| `backend/main.py`, `backend/static/` | FastAPI + dashboard (map, lab builder, review, history) |
| `agent.py` | Terminal front end: labs, changes, plain language, users, drift |
| `tests/` | 40 scenarios offline/live + live end-to-end on generated labs |

## Setup (macOS, one time)

Containerlab needs Linux, so the routers run in an OrbStack Linux machine called `clab`. The agent runs on the Mac and reaches them through `orb -m clab docker …`.

**1.** Install OrbStack (`brew install orbstack`), open it once, then create the machine:

```bash
orb create ubuntu clab
```

**2.** Install Docker and Containerlab inside it:

```bash
orb -m clab bash -c 'curl -sL https://containerlab.dev/setup | sudo -E bash -s "all"'
```

**3.** Pull the FRR router image:

```bash
orb -m clab docker pull quay.io/frrouting/frr:10.2.1
```

**4.** Set up Python on the Mac:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

**5.** Deploy the demo lab and run the tests:

```bash
.venv/bin/python agent.py lab deploy triangle-demo
```

```bash
.venv/bin/python -m pytest -q
```

**6.** Optional, for plain language in any language: copy `.env.example` to `.env` and fill in your OpenAI-compatible endpoint, key and model. Without it, the offline grammar still handles English and common Hinglish phrasing.

## Daily use

Start the dashboard at http://localhost:8000:

```bash
.venv/bin/uvicorn backend.main:app --port 8000
```

On first start it creates the user `admin` and writes the password to `data/initial-admin-password`. Add people from the Labs tab, or from the terminal:

```bash
.venv/bin/python agent.py user add ishika --role approver
```

Roles:
- **operator:** proposes changes.
- **approver:** also approves or rejects them. Nobody can approve their own change.
- **admin:** also manages labs and users, and can accept drift.

Terminal equivalents (`python agent.py --help` lists all):

```bash
.venv/bin/python agent.py lab new ring8 --template ring --routers 8
```

```bash
.venv/bin/python agent.py lab deploy ring8
```

```bash
.venv/bin/python agent.py ask "connect R1 to R5"
```

## Lab file format

Minimal; addresses, interfaces and starting routes are derived:

```yaml
name: campus
routers: [R1, R2, R3, R4]
links: [[R1, R2], [R2, R3], [R3, R4], [R4, R1], [R1, R3]]
baseline: shortest-path        # or none: start empty and build routing with CONNECT
```

Optional: per-router LANs (`R3: {lans: [10.10.30.1/24]}`), fixed link subnets (`{a: R1, b: R2, subnet: 10.0.12.0/30}`), extra `routes`, and a saved map `layout`. See `labs/triangle-demo/lab.yaml`. Limit: 30 routers per lab (4 GB VM).

## Intents

| Intent | Example | What the agent does |
|---|---|---|
| `ADD_ROUTE` | R1, 10.10.31.0/24, next hop R3 | One static route; next hop must be a direct neighbour |
| `REMOVE_ROUTE` | R1, 10.10.30.128/25 | Looks up the existing next hop, removes it |
| `CONNECT` | R1 ⇄ R4 | Shortest path over the links; adds only the routes that are missing on each hop, in both directions; one transaction |

## Demo script (about 7 minutes)

1. **Labs tab:** draw a 5-router lab (or pick a template), "Create and deploy". The map draws itself and the reachability table shows 0 paths.
2. **Plain language:** type "R1 ko R4 tak pahunchao" and click Understand. Check the CONNECT card and validate it. Blue arrows show the routes on every hop. Approve as a different user; the table shows the new paths.
3. **Separation of duties:** as the requester, the dashboard refuses to let you approve your own change.
4. **Loop / blackhole:** use the generated example buttons. The twin blocks them and draws the path in red or amber.
5. **Failed deploy:** approve with the failure test "A link goes down after deploy". The dead link is detected, every router is restored, and the status is ROLLED_BACK.
6. **Drift:** on a router, `vtysh -c 'conf t' -c 'ip route 10.10.99.0/24 <neighbour IP>'`. The banner appears and new changes are blocked. "Revert through approval", then approve.
7. **Broken lab:** `orb -m clab docker restart clab-<lab>-r1`, then submit a change. Pre-flight refuses it and names the missing interfaces. Redeploy the lab.
8. **History and tests:** `python -m pytest -q`, which runs 57 checks offline.

## Tests

| File | What | How to run |
|---|---|---|
| `tests/test_scenarios.py` | The original 20 scenarios: 4 PS categories, happy paths, injection, audit | offline; `NMA_LIVE=1` on real routers |
| `tests/test_round2.py` | 20 more: templates, addressing, CONNECT, plain language, LLM validation, pre-flight, drift, login/roles, API identity, examples, speed | offline; `NMA_LIVE=1` on real routers |
| `tests/test_live_labs.py` | Generate ring/tree/partial-mesh labs, deploy, CONNECT, forced rollback, destroy | `NMA_LIVE_LABS=1` |

Results: `docs/test-results.txt` (57 offline, 57 live, 3 generated labs live, all passing).

## Security principles

| Principle | Where |
|---|---|
| Least privilege | Three intents only; roles operator / approver / admin (`auth.py`, `main.py`) |
| No arbitrary commands | Templates + strict allowlist (`generator.py`); LLM output is intents, never CLI (`nlp.py`) |
| Input validation | Pydantic, `extra="forbid"`, typed IPs, safe-text patterns, lab-aware router names (`schema.py`) |
| Test before change | Pre-flight lab health, digital twin, FRR `vtysh --dryrun` (`deploy.py`, `twin.py`, `lab.py`) |
| Human-in-the-loop | Approval required; timeout = reject; requester can never approve their own change (`engine.py`) |
| Authentication | scrypt password hashes, HttpOnly session cookie, identity taken from the session, not the request (`auth.py`) |
| Safe recovery | Git-committed snapshots, all-router rollback, re-verified after rollback (`deploy.py`) |
| Consistency | Drift detection blocks changes while routers differ from the approved state (`engine.py`) |
| Auditability | Every stage, every plain-language request, every login-protected action; SHA-256 hash chain (`audit.py`) |
| Isolation | Lab only, single vendor (FRR), next hops must be lab routers |
| Untrusted AI | LLM output validated by the same schema; unknown fields and routers dropped; human approves |

## Limits and future work

- One lab runs at a time; one change at a time per lab (global lock).
- The twin models static routing only; OSPF/BGP would need a protocol model (or Batfish).
- Sessions live in memory: restarting the server signs everyone out.
- Real devices would need a transport adapter (NETCONF, RFC 6241; YANG, RFC 7950) in place of `docker exec`.
