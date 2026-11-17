# Pseudocode for the mentor

Each line names the file that implements it.

```
load the active lab from labs/<name>/lab.yaml                                backend/topology.py
    addresses, interfaces, links, baseline routes are derived, not hand-written

receive a request                                       dashboard / agent.py / POST /api/intent, /api/ask
    plain language? -> offline grammar, else LLM -> proposed intent JSON      backend/nlp.py
                       (the LLM never writes commands; a human checks the proposal)
    requester = the signed-in user (never taken from the request body)        backend/main.py

validate schema (Pydantic, against this lab's routers)  -> if invalid: log INVALID, STOP   backend/schema.py
pre-flight: every interface present, up, addressed; every link passes traffic
                                                        -> if not: BLOCKED, STOP          backend/deploy.py
drift: do the routers match the last approved state?    -> if not: BLOCKED, STOP          backend/engine.py
semantic checks                                         -> if fail: BLOCKED, STOP         backend/checks.py
    ADD: next hop is a neighbour, no conflict, no duplicate
    REMOVE: route exists, look up its next hop
    CONNECT: shortest path over the links; on each hop that does not already deliver,
             add a route to the next router; conflicting route on the path -> BLOCKED
generate candidate from Jinja2 templates                                      backend/generator.py
allowlist: every line is exactly "[no ]ip route <prefix> <ip>"  -> else BLOCKED, STOP
twin: copy the route tables, apply the candidate to the copy                  backend/twin.py
    for every destination, from every router, follow next hops (longest prefix match)
    new LOOP? new BLACKHOLE? lost reachability?  -> BLOCKED, STOP (path is named and drawn)
FRR syntax check: vtysh --dryrun on every changed router  -> if fail: BLOCKED, STOP       backend/lab.py
status = PENDING_APPROVAL; show intent, diff per router, report, map arrows

approver = a signed-in approver who is not the requester                      backend/engine.py
if reject, or no decision within 5 minutes  -> REJECTED, STOP
pre-flight again; routes must still equal those validated  -> else BLOCKED, STOP
snapshot running-config of every router, git commit                           backend/deploy.py
try:
    deploy to all routers (any router failing fails the whole change)
    health: every link still passes traffic, routes equal the twin's prediction,
            every LAN pair the twin predicts can ping
    record the new approved state; SUCCESS
except failure:
    restore the snapshot on every router (frr-reload), health check again
    ROLLED_BACK, or ROLLBACK_FAILED and alert the operator
every step appends a hash-chained row to the SQLite audit log                 backend/audit.py
```
