# Pseudocode for the mentor

Each line names the file that implements it.

```
receive intent (JSON)                                     agent.py / backend/main.py  POST /api/intent
validate schema (Pydantic)        -> if invalid: log INVALID, STOP          backend/schema.py
read live static routes from R1, R2, R3 (vtysh)                             backend/lab.py
semantic checks                   -> if fail: log BLOCKED, STOP             backend/checks.py
    next-hop is a directly connected neighbour
    ADD: no route for the prefix yet (conflict / duplicate)
    REMOVE: route exists; look up its next-hop
generate candidate from Jinja2 template                                     backend/generator.py
allowlist: every line is exactly "[no ]ip route <prefix> <ip>"  -> else BLOCKED, STOP
twin: copy the route tables, apply the candidate to the copy                backend/twin.py
    for every destination, from every router, follow next-hops (longest prefix match)
    new LOOP?            -> BLOCKED, STOP (names the path, e.g. R1 → R2 → R3 → R1)
    new BLACKHOLE?       -> BLOCKED, STOP
    lost reachability?   -> BLOCKED, STOP
FRR syntax check: vtysh --dryrun on the candidate   -> if fail: BLOCKED, STOP   backend/lab.py
status = PENDING_APPROVAL, show intent + diff + report to a human           backend/engine.py
if reject, or no decision within 5 minutes -> log REJECTED, STOP
re-read routes; if the network changed since validation -> BLOCKED, STOP
snapshot running-config of all 3 routers, git commit                        backend/deploy.py
try:
    deploy to all routers (any router failing fails the whole change)
    health check: routes equal the twin's prediction, every predicted LAN ping works
    log SUCCESS
except failure:
    restore the snapshot on all 3 routers (frr-reload)
    health check against the old state
    log ROLLED_BACK, or ROLLBACK_FAILED and alert the operator
every step above appends a hash-chained row to the SQLite audit log         backend/audit.py
```
