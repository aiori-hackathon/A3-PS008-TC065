# PS-008 slide content

Follows `Proposed-structure-hackathon.pdf` slide for slide (11 slides).
Items in `<angle brackets>` are yours to fill in.

---

## Slide 1 — Title

**A3-PS008-<TC number>**

Network Management Agent for Reviewed Configuration Changes

**<Member 1 name>, <Member 2 name>**
**<email>**

AIORI Hackathon 3
<date>

---

## Slide 2 — Problem Description

*Header tab: Abstract*

**Manual router configuration is a leading cause of avoidable network outages.**

- Engineers type commands directly into router CLIs, so a single typo or wrong next-hop reaches the live network immediately.
- A change that is valid on one router can conflict with routes on another, creating a routing loop or a blackhole that no single device reports as an error.
- There is usually no test step between "command written" and "command applied".
- Changes are often applied without a second person reviewing them, and without a record of who approved what and why.
- When a change goes wrong, recovery depends on someone remembering the previous configuration.

**Goal:** an agent that turns a small set of operator intents into router configuration, proves the change is safe in a digital twin, waits for human approval, and rolls back automatically if deployment fails.

---

## Slide 3 — Background

*Header tab: Background*

**The ideas this project builds on**

- **Intent-Based Networking (IBN):** the operator states the outcome ("R1 should reach the R3 LAN via R2") and software derives the device commands [1].
- **Static routing and next-hops:** each router forwards a packet to the next-hop listed for the destination prefix. Correctness is a property of all routers together, not of one.
- **Routing loop:** following next-hops for a destination returns to a router already visited. **Blackhole:** the walk reaches a router with no route for that destination.
- **Digital twin:** a software model of the network on which a change can be applied and checked before any real device is touched [4].
- **Candidate vs running configuration:** NETCONF separates a proposed configuration from the active one and supports validate, commit and rollback [2][5]. We reproduce that workflow for a platform without NETCONF.
- **Human-in-the-loop:** automation prepares and checks the change; a person makes the final decision.

---

## Slide 4 — Literature & RFC Review

*Header tab: Literature & RFC Review*

**Most relevant standards and prior work**

- **RFC 9315, Intent-Based Networking: Concepts and Definitions [1].** Defines intent and the intent lifecycle (translate, verify, deploy, assure). Our pipeline follows this lifecycle.
- **RFC 6241, NETCONF [2].** Candidate datastore, `validate`, confirmed commit and rollback-on-error. Source of our candidate, validate, approve, commit, rollback model.
- **RFC 7950, YANG 1.1 [3].** Schema language for configuration data. Our intent schema is a typed, constrained tree in the same style.
- **Network Digital Twin architecture, IRTF NMRG [4].** Motivates validating changes on a model before the real network.
- **Batfish [6] and Header Space Analysis [7].** Show that loops and blackholes can be found by analysing configuration offline, before deployment. Our twin applies the same idea at the scale of three routers and static routes.

**Gap we address:** these tools verify configuration, but they do not combine intent translation, verification, an explicit human approval gate, and automatic all-router rollback in one auditable flow.

---

## Slide 5 — Proposed Solution

*Header tab: Proposed Solution*

**A human-reviewed agent that takes an intent from request to verified deployment**

Lab: 3 FRRouting routers (R1, R2, R3) in a triangle, run with Containerlab [8][9]. Two intents: `ADD_ROUTE` and `REMOVE_ROUTE`.

Every request passes through 9 stages and can be stopped at any of them:

1. **Schema check** — typed intent (router, destination prefix, next-hop, requester, reason).
2. **Semantic check** — next-hop directly connected, no conflict or duplicate, route exists for removal.
3. **Config generation** — fixed templates plus a command allowlist; the operator never types CLI.
4. **Digital twin** — apply to a copy of the model; check loops, blackholes, reachability, and FRR syntax.
5. **Validation report** — one PASS/FAIL line per check.
6. **Human approval** — intent, config diff and report shown; approve or reject; no answer in 5 minutes counts as reject.
7. **Backup** — running-config of all 3 routers saved and committed to git.
8. **Deploy** — all 3 routers treated as one transaction.
9. **Health check** — pass marks SUCCESS; fail restores all 3 routers and marks ROLLED BACK.

Every stage is written to an audit log.

---

## Slide 6 — Optimization Proposed

*Header tab: Solution Optimization*

**Manual CLI change vs the agent**

| Aspect | Before: manual CLI | After: this agent |
|---|---|---|
| Operator input | Free-form commands | Structured intent, 2 allowed types |
| Syntax errors | Found when the router rejects the line | Rejected by schema and FRR parser before approval |
| Conflicting routes | Found after traffic breaks | Blocked by semantic check |
| Loops and blackholes | Found by users or traceroute | Blocked by the digital twin |
| Review | Optional, informal | Mandatory approval gate with diff; timeout means reject |
| Command scope | Anything the CLI accepts | Allowlist: `ip route` / `no ip route` only |
| Failed change | Manual recovery from memory | Automatic rollback of all 3 routers from snapshot |
| Partial failure | Network left half-changed | All-or-nothing across routers |
| Record | Shell history, if any | Audit record per stage: who, what, when, why |

**Further enhancements:** fault-injection flag to prove rollback on demand; optional LLM that only converts plain English into an intent, which then passes through the same pipeline.

---

## Slide 7 — Solution Architecture & Design

*Header tab: Architecture Design (UML Diagram)*

**Figure: System Architecture UML Diagram** *(image to be added)*

Components the diagram must show:

- **Operator** → **Dashboard / CLI** (`agent.py`)
- **API layer** (FastAPI)
- **Engine**: Intent Schema → Semantic Checker → Config Generator + Allowlist → Digital Twin → Approval Manager → Deployer → Health Checker → Rollback
- **Stores**: Inventory (`inventory.yaml`), Audit DB (SQLite), Snapshots (git)
- **Lab**: R1, R2, R3 (FRR in Containerlab), reached only by the Deployer and the route reader
- Optional **LLM** box feeding the Intent Schema only, with no line to the routers

---

## Slide 8 — Data Flow Diagram

*Header tab: Data Flow Diagram*

**Figure: Data Flow Diagram (DFD)** *(image to be added)*

Flow the diagram must show:

1. Operator → intent JSON → Schema check → (invalid: log, stop)
2. → Semantic check, reading live routes from the routers → (fail: log, stop)
3. → Candidate config → Allowlist
4. → Digital twin (model copy) → Validation report → (fail: log BLOCKED, stop)
5. → Human: intent + diff + report → (reject or timeout: log REJECTED, stop)
6. → Snapshot of R1, R2, R3 → git
7. → Deploy to routers → Health check
8. → pass: SUCCESS · fail: restore snapshot on all 3 routers → ROLLED BACK

Every arrow that stops also writes to the Audit DB.

---

## Slide 9 — Deployment Timeline

*Header tab: Deployment Timeline*

**Methodology:** build the safety core first and prove each stage before starting the next. Dashboard and LLM come last. Each milestone has a pass condition.

| Days | Milestone | Done when |
|---|---|---|
| 1 | Environment: Containerlab, Docker, FRR image, repo | Lab tooling runs on both laptops |
| 1–3 | 3-router lab, static routes configured by hand | Every LAN pings every other LAN |
| 3–5 | Intent schema, semantic checks, config generator | Valid intent yields the right command; invalid ones give a clear reason |
| 5–8 | Digital twin: loop, blackhole, reachability, syntax | A looping intent is blocked and the path is named |
| 8–10 | Snapshot, deploy, health check, rollback | A forced failure restores all 3 routers |
| 10–11 | Integration: terminal flow and API | Four demo scenarios run end to end |
| 11–14 | Dashboard and test suite | At least 12 scenarios pass across the 4 PS categories |
| 14–16 | Report, demo rehearsal, submission | Report in repo, pseudocode shared with mentor |

---

## Slide 10 — Bibliography

[1] A. Clemm, L. Ciavaglia, L. Z. Granville, J. Tantsura. "Intent-Based Networking - Concepts and Definitions." RFC 9315, IRTF, October 2022.

[2] R. Enns, M. Bjorklund, J. Schoenwaelder, A. Bierman (eds.). "Network Configuration Protocol (NETCONF)." RFC 6241, IETF, June 2011.

[3] M. Bjorklund (ed.). "The YANG 1.1 Data Modeling Language." RFC 7950, IETF, August 2016.

[4] C. Zhou et al. "Network Digital Twin: Concepts and Reference Architecture." Internet-Draft draft-irtf-nmrg-network-digital-twin-arch, IRTF NMRG. <check latest version and date>

[5] M. Bjorklund et al. "Network Management Datastore Architecture (NMDA)." RFC 8342, IETF, March 2018.

[6] A. Fogel et al. "A General Approach to Network Configuration Analysis." In: USENIX NSDI, 2015.

[7] P. Kazemian, G. Varghese, N. McKeown. "Header Space Analysis: Static Checking for Networks." In: USENIX NSDI, 2012.

[8] FRRouting Project. "FRRouting User Guide." https://docs.frrouting.org

[9] Containerlab. "Containerlab Documentation." https://containerlab.dev

---

## Slide 11 — Closing

Thank You for Your Attention!
