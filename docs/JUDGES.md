# PS-008 judge pack

Everything a judge needs to check our work, and everything we need on the day.

1. [Every requirement, with proof](#1-every-requirement-with-proof)
2. [Honest limits](#2-honest-limits)
3. [Five-minute run of show](#3-five-minute-run-of-show)
4. [Morning checklist and backup plans](#4-morning-checklist-and-backup-plans)
5. [Hard questions and our answers](#5-hard-questions-and-our-answers)

---

## 1. Every requirement, with proof

Each row is one line of the PS-008 problem statement. **Proof** is something a judge can see on screen or run themselves. Test numbers refer to `tests/` (for example, test 07 is `test_07_conflict_same_prefix_other_next_hop`).

### Key requirements

| # | PS-008 asks for | Status | Where it is built | Proof |
|---|---|---|---|---|
| K1 | One router platform and a 3-router lab testbed | Met | FRR 10.2.1 only. The 3-router testbed is `labs/triangle-demo/lab.yaml` | `agent.py lab deploy triangle-demo` then `agent.py status` shows 3 routers and GO. We go further: any topology up to 30 routers, still FRR only |
| K2 | 2 mentor-defined configuration intents | Met | `ADD_ROUTE` and `REMOVE_ROUTE` in `backend/schema.py` | `agent.py schema` writes `docs/intent-schema.json`. Tests 01, 02. Our extra `CONNECT` intent compiles into the same two commands |
| K3 | A structured intent schema | Met | Pydantic (`backend/schema.py`) and a YANG 1.1 model (`yang/nma-intent.yang`, RFC 7950) | `.venv/bin/pyang --strict --lint yang/nma-intent.yang` passes. Tests 46, 47, 48 |
| K4 | Validate candidate configurations before deployment | Met | Semantic checks (`checks.py`), digital twin for loops, blackholes and reachability (`twin.py`), FRR's own syntax dry-run (`lab.py`), lab pre-flight (`deploy.py`) | Dashboard: a loop example is blocked and drawn in red. Tests 07, 08, 17, 32 |
| K5 | A clear human approval gate | Met | `backend/engine.py`: every change waits in `PENDING_APPROVAL`. A different person must approve. No answer in 5 minutes means rejected | Dashboard refuses to let the requester approve their own change. Tests 09, 10, 31, 37 |
| K6 | Automatic rollback after a failed deployment | Met | `backend/deploy.py`: snapshot of every router (committed to git) before deploy, restore all routers on any failure, then re-check | Dashboard failure test "A link goes down after deploy" ends `ROLLED_BACK`. Tests 11, 12, 19, 33, 43 |
| K7 | At least 12 tests, including conflicting routes, invalid configurations, rejected approvals and failed deployments | Met | `tests/`: 48 scenarios, 72 checks | `.venv/bin/python -m pytest -q` prints `72 passed, 3 skipped`. Conflicting routes: 07, 14, 27. Invalid configurations: 03, 04, 05, 06, 13, 16, 47. Rejected approvals: 09, 10, 31. Failed deployments: 11, 12, 33, 43 |
| K8 | LLM use is optional | Met | `backend/nlp.py`: offline grammar first, LLM only if configured | With no internet, "connect R1 to R4" and "R1 ko R3 tak pahunchao" still work. Tests 28, 29, 30, 41 |
| K9 | No multi-vendor support, no unrestricted commands, no production network access | Met | FRR only. Only `ip route` and `no ip route` pass the allowlist in `backend/generator.py`. Routers live in an isolated lab VM and next hops must be lab routers | `agent.py yang-check intents/6-injection.json` prints `NOT valid`. Test 13 (command injection) |

### Expected outcomes

| # | Expected outcome | Status | Where it is |
|---|---|---|---|
| O1 | Network management agent | Met | `agent.py` (terminal) and the dashboard (`backend/main.py`, `backend/static/`) |
| O2 | Structured intent schema | Met | See K3 |
| O3 | Candidate configuration generator | Met | `backend/generator.py`: Jinja2 templates plus the command allowlist |
| O4 | Lab validation and testing framework | Met | Digital twin, pre-flight, FRR dry-run, and the `tests/` suite |
| O5 | Human approval workflow | Met | See K5. Also roles (operator, approver, admin) and confirmed commit |
| O6 | Deployment and rollback mechanism | Met | See K6 |
| O7 | Test results covering at least 12 scenarios | Met | `docs/test-results.txt`: 72 checks pass offline (re-run 2026-11-24) and 72 on real FRR routers (2026-11-17). 3 of 3 generated labs pass end to end (2026-11-15) |
| O8 | Final demonstration and technical report | Met | Demo video https://youtu.be/j9w14NcGQWg, the deck PDF in the repo, `README.md` and this pack |

### Deliverables and references

| # | Item | Status | Proof |
|---|---|---|---|
| D1 | GitHub collaboration, repo owned by aiori-hackathon | Met | `gh api repos/aiori-hackathon/A3-PS008-TC065 --jq .owner.login` prints `aiori-hackathon` |
| R1 | NETCONF (RFC 6241) | Met, partly | Confirmed commit (§8.4) and the candidate, validate, commit flow are implemented (tests 42 to 45). The transport to routers is `docker exec`, not a NETCONF session (see limits) |
| R2 | YANG 1.1 (RFC 7950) | Met | `yang/nma-intent.yang`; every request is validated against it (`backend/yangcheck.py`) |

---

## 2. Honest limits

Say these before a judge finds them. Each limit is a design choice, not a hidden bug.

- **Lab only, FRR only, IPv4 static routes only.** That is the scope PS-008 sets.
- **The twin models static routing.** It does not model OSPF or BGP, ACLs or traffic load. A protocol model or Batfish would be the next step.
- **Transport is `docker exec vtysh`, not NETCONF or SSH.** The YANG model and confirmed commit follow the standards. Only `backend/lab.py` talks to routers, so changing the transport means changing one file.
- **One lab and one change at a time.** A global lock keeps changes in order.
- **Sessions live in memory.** Restarting the server signs everyone out.
- **The language model can misunderstand.** That is why it only fills in an intent card for a person to check, and every check still runs afterwards.
- **The audit log is tamper-evident, not tamper-proof.** Someone with full database access could rewrite the whole chain. Production would store the latest hash somewhere else.
- **Tested in our lab on one laptop,** not on physical routers.

**Four lines that must be said, even if a beat is cut:**

1. "The AI only translates. It never writes a command and never touches a router."
2. "Only two commands can ever reach a router: `ip route` and `no ip route`."
3. "Nothing deploys without a second person's approval."
4. "These are real FRR routers in a lab, not a production network."

---

## 3. Five-minute run of show

Demo lab: **campus5** (5 routers in a ring, no routes at the start). Two browser windows: Mukul logged in as an operator, Ishika as an approver in a private window.

| Time | Beat | Speaker | Covers | Screen |
|---|---|---|---|---|
| 0:00–0:30 | The problem: typing commands straight into routers causes outages, and nothing checks the whole network first | Mukul | Problem | Slide 2 |
| 0:30–1:00 | Our answer in one line, and the 8 stages | Mukul | O1 | Slides 3 and 4 |
| 1:00–2:00 | In the operator window, type "R1 ko R3 tak pahunchao". The agent understands it offline as CONNECT R1 ⇄ R3 and shows the card. Validate: all checks green, blue route arrows on the map. Try to approve from the same window: refused. Approve from the approver window. The reachability table updates | Ishika | K2, K3, K5, K8 | Dashboard, Review |
| 2:00–2:45 | Click the "Create a loop" example (it appears once the lab has routes, so after the CONNECT). The twin blocks it and draws the loop in red. Nothing reached a router. **Say line 2** | Mukul | K4, K9 | Dashboard map |
| 2:45–3:45 | Click the "Add a route" example, validate, then approve it with the failure test "A link goes down after deploy". The health check fails, every router is restored, status `ROLLED_BACK`. **Say line 3** | Ishika | K6 | Review page |
| 3:45–4:20 | History screen, then `agent.py verify-audit` (chain intact) and `pytest -q` (72 passed) | Mukul | K7, O7 | History, terminal |
| 4:20–5:00 | Limits (**lines 1 and 4**), who it helps, the repo and video links | Ishika | Limits, impact | Slides 11 and 12 |

If you have 10 minutes, add these from the README demo script: drift detection (step 6), confirmed commit (step 7) and the broken-lab pre-flight (step 8).

---

## 4. Morning checklist and backup plans

### Checklist, one hour before

1. Start the lab VM if it is off: `orb start clab`
2. Deploy the demo lab. This also clears any leftover state from a VM restart: `.venv/bin/python agent.py lab deploy campus5`
3. Run the GO / NO-GO check: `.venv/bin/python agent.py status`. It must print **GO**. If it prints NO-GO, it also prints the fix.
4. Run the tests once so you know they pass on this laptop: `.venv/bin/python -m pytest -q` → `72 passed, 3 skipped`
5. Start the dashboard: `.venv/bin/uvicorn backend.main:app --port 8000`. Log in as the operator in one window and as the approver in a private window.
6. Check History for changes waiting for approval. Only one change can run at a time, and a waiting one expires after 5 minutes.
7. Download the demo video to the laptop. Open the deck PDF. Turn on Do Not Disturb. Pack the charger and an HDMI adapter.
8. Use only the phrasings that work offline (no internet needed): "connect R1 to R4", "make R4 reachable from R2", "R1 ko R3 tak pahunchao". Use any other language only if the venue internet works.

### Backup plans

Say which plan you are switching to, out loud. Suggested line: **"We'll switch to the recorded run. The evidence is the same, only the screen changes."** Never claim a live run that did not happen.

| Plan | When | What to do |
|---|---|---|
| **A: live** (default) | `agent.py status` says GO and the dashboard loads | Follow the run of show |
| **B: lab breaks during the demo** | A change is BLOCKED by pre-flight, or the map shows red links | Turn it into a feature: "This is our pre-flight check. It refuses to deploy onto a broken network." Then `agent.py lab deploy campus5` and continue, or switch to plan C |
| **C: no lab** | The VM or containerlab will not start | `pytest -q` needs no routers, so run it live. Play the downloaded demo video. Walk the screenshots in the deck |
| **D: no laptop or projector** | Nothing will display | Deck PDF from a phone or pen drive, the printed judge sheet (`docs/judge-sheet.pdf`), and the four lines above |

---

## 5. Hard questions and our answers

**1. What if the AI misunderstands a request?**
It only fills in an intent card. A person sees and can edit the card, then the request goes through the schema, the YANG model, the twin and the approval gate like any other. Unknown fields and routers are dropped. *Test 30.*

**2. Can anyone inject a router command?**
No. Commands come only from fixed templates, and an allowlist lets through only `ip route` and `no ip route`. Inputs are typed IP addresses and safe-text patterns, checked twice (Pydantic and YANG). *Test 13; `agent.py yang-check intents/6-injection.json` prints NOT valid.*

**3. Why only static routes and two intents?**
PS-008 asks for two mentor-defined intents on one platform. We went deep rather than wide: CONNECT shows that a higher-level goal compiles into the same two safe commands across many routers in one transaction.

**4. How do you know the twin is right?**
It reads the real routes from the routers and follows each packet with longest-prefix match, the same lookup a router does. After deploying, we ping every LAN pair the twin predicted on the real routers (42 pings on a 7-router tree). If reality disagrees, everything rolls back. *Test 40.*

**5. What if the approver is careless?**
Approval is one layer of several. The twin, the FRR dry-run, the health check and the automatic rollback still run. The audit log records who approved and why.

**6. Can someone approve their own change?**
No. The approver's identity comes from the login session, not from the request, and the requester is refused. *Tests 31, 37.*

**7. What if nobody approves?**
The change is rejected automatically after 5 minutes. *Test 10.*

**8. What if deployment fails halfway?**
Every router's running config is snapshotted and committed to git before deploy. On any failure all routers are restored, and the restore itself is checked. *Tests 11, 19.*

**9. What if the pings pass but something else broke?**
The health check covers every link and interface, not just the path we changed. *Test 33: a dead link elsewhere still triggers rollback.*

**10. What if someone changes a router by hand?**
Drift detection compares the live routes with the last approved state. New changes are blocked until the drift is reverted through approval, or an admin accepts it, which is logged. *Tests 34, 35.*

**11. What if the agent itself crashes during a deploy?**
The snapshot is already in git, so the previous state can be restored. After a restart, drift detection sees that the routers differ from the approved state and blocks new changes. We do not have an automatic crash-recovery journal; that is future work.

**12. Is the audit log tamper-proof?**
It is tamper-evident: each record carries the SHA-256 hash of the one before it, so any edit breaks the chain. `agent.py verify-audit` prints "audit chain intact". Someone with full database access could rewrite the whole chain, so production would store the latest hash outside the database. *Test 20.*

**13. Would this work on real routers, or on Cisco?**
Only `backend/lab.py` talks to routers. Swap `docker exec` for a NETCONF client (ncclient) or SSH, and keep everything else. `ip route` has the same syntax on Cisco IOS. The intents are already a YANG model.

**14. Does it scale?**
The twin checks a 30-router lab in under a second, and routers are read in parallel. *Test 39.* Labs are capped at 30 routers by the VM's memory, not by the agent.

**15. What about OSPF and BGP?**
Not modelled today. The twin would need a protocol model, or a tool such as Batfish. This is the main next step.

**16. How is this different from Ansible?**
Ansible applies what you write. It does not prove the whole network stays loop-free before pushing, it does not force a second person to approve, and it does not verify and roll back every router by itself. Our agent could sit in front of Ansible.

**17. Why containers and not real routers?**
PS-008 says lab only. FRR is production routing software used in real networks, and the containers run the real routing daemons and the real `vtysh` CLI.

**18. Does the AI see our network, and what does it cost?**
The lab's addresses are never sent to the AI. The prompt is small, the answer is capped at 60 tokens, repeated sentences are cached, and there is a daily limit. Without any AI, the offline grammar still works. *Test 41.*

**19. Why confirmed commit?**
It comes from NETCONF (RFC 6241 §8.4). A change can look fine yet cut off the operator. With confirmed commit the change undoes itself unless someone confirms it in time. *Tests 42 to 45.*

**20. What is not finished?**
Everything in [section 2](#2-honest-limits). We would rather show what is partial than have you find it.
