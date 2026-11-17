"""Terminal front end for the agent (also the demo fallback if the dashboard breaks).

Labs
  python agent.py lab list                                  saved labs, which one is active and running
  python agent.py lab new ring6 --template ring --routers 6 create labs/ring6/lab.yaml from a template
  python agent.py lab deploy ring6                          start it (stops any other lab) and make it active
  python agent.py lab show [name]                           routers, links, addresses
  python agent.py lab use ring6 | lab destroy ring6

Changes (on the active lab)
  python agent.py status                                    routes and who reaches which LAN
  python agent.py apply intents/1-add-route.json [--simulate-failure deploy|health [--failure-router R3]]
  python agent.py ask "connect R1 to R4"                    plain language (offline grammar, else the configured LLM)
  python agent.py history | show <change_id> | reset | schema | verify-audit
  python agent.py drift [show|revert|accept]                changes made on the routers outside the agent

People (for the dashboard sign-in)
  python agent.py user add alice --role approver | user list
"""
import argparse
import getpass
import json
import select
import sys
import time

from backend import auth, labctl, labgen, nlp, schema
from backend.engine import db_path, default_engine
from backend.topology import ROOT, Topology, lab_names

COLOR = {"PASS": "\033[32m", "SUCCESS": "\033[32m", "+": "\033[32m", "-": "\033[31m", "FAIL": "\033[31m",
         "INVALID": "\033[31m", "BLOCKED": "\033[31m", "REJECTED": "\033[33m", "ROLLED_BACK": "\033[33m",
         "ROLLBACK_FAILED": "\033[41m", "PENDING_APPROVAL": "\033[36m"}
END = "\033[0m"


def paint(text, key):
    return f"{COLOR.get(key, '')}{text}{END}" if sys.stdout.isatty() else text


def describe(c):
    i = c.get("normalized") or c["intent"]
    if not isinstance(i, dict):
        return str(i)
    if i.get("intent") == "CONNECT":
        return f"CONNECT {i.get('source')} ⇄ {i.get('destination')}" if i.get("both_ways", True) else f"CONNECT {i.get('source')} → {i.get('destination')}"
    return f"{i.get('intent')} {i.get('router')} {i.get('destination')} {i.get('next_hop', '')}".strip()


def print_change(c):
    print(f"\nChange {c['id']} on lab {c.get('lab', '?')}  status: {paint(c['status'], c['status'])}")
    print(f"  {c['reason']}")
    for r in c.get("report", []):
        result = "PASS" if r["ok"] else "FAIL"
        print(f"  [{paint(result, result)}] {r['check']:<12} {r['detail']}")
    for router, lines in c.get("diff", {}).items():
        print(f"  {router} static routes:")
        for line in lines:
            print("    " + paint(line, line[0]))


def cmd_status(args):
    n = default_engine().network()
    print(f"Lab {n['name']} ({n['template']}): {len(n['routers'])} routers, {len(n['links'])} links")
    for r, info in n["routers"].items():
        print(f"{r}  LANs: {', '.join(info['lans'])}")
        for prefix, nh in info["static"]:
            print(f"    ip route {prefix} {nh}")
    print("\nReachability (twin model of the live routes):")
    for x in n["reachability"]:
        print(f"  {x['src']} → {x['dst']:<16} {paint(x['outcome'], 'PASS' if x['outcome'] == 'DELIVERED' else 'FAIL')}"
              f"  {' → '.join(x['path'])}")


def cmd_apply(args):
    return decide(default_engine(), json.loads(open(args.file).read()), args)


def cmd_ask(args):
    """Plain language -> intents (offline grammar, else the configured LLM) -> the normal pipeline."""
    eng = default_engine()
    r = nlp.understand(" ".join(args.text), eng.topo)
    if not r["intents"]:
        print(r["note"])
        return 1
    print(f"Understood by {r['how']}:" + (f"  ({r['note']})" if r["note"] else ""))
    for i in r["intents"]:
        print("  " + json.dumps(i, ensure_ascii=False))
    status = 0
    for i in r["intents"]:
        status |= decide(eng, {**i, "requested_by": args.requested_by}, args) or 0
    return status


def decide(eng, raw, args, change=None):
    c = change or eng.plan(raw)
    print_change(c)
    if c["status"] != "PENDING_APPROVAL":
        return 1
    left = int(c["expires_at"] - time.time())
    print(f"\nApprove and deploy as {args.approver}? Type 'yes' to approve, anything else rejects. "
          f"Auto-reject in {left}s: ", end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], left)
    if not ready:
        print()
        c = eng.get(c["id"])  # expiry turns it into REJECTED
    else:
        answer = sys.stdin.readline().strip().lower()
        print("Reason: ", end="", flush=True)
        reason = sys.stdin.readline().strip()
        try:
            if answer == "yes":
                print("Pre-flight, backup, deploy, health check...")
                c = eng.apply(c["id"], args.approver, reason, args.simulate_failure, args.failure_router)
            else:
                c = eng.reject(c["id"], args.approver, reason)
        except ValueError as e:
            print(f"\nRefused: {e}")
            return 1
    print(f"\nResult: {paint(c['status'], c['status'])}\n  {c['reason']}")
    return 0 if c["status"] in ("SUCCESS", "REJECTED") else 1


def cmd_history(args):
    for c in default_engine().list():
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(c["created_at"]))
        print(f"{c['id']}  {when}  {c.get('lab', '?'):<14} {paint(f'{c['status']:<16}', c['status'])} {describe(c)}")


def cmd_show(args):
    eng = default_engine()
    c = eng.get(args.change_id)
    if not c:
        print("no such change")
        return 1
    print_change(c)
    print("\nAudit trail:")
    for e in eng.db.events(c["id"]):
        print(f"  {e['ts']}  {e['stage']:<18} {e['result']:<10} {e['detail'][:120]}")


def cmd_reset(args):
    for r, (ok, out) in default_engine().reset().items():
        print(f"{r}: {'restored to baseline' if ok else 'FAILED ' + out}")


def cmd_schema(args):
    path = ROOT / "docs" / "intent-schema.json"
    path.write_text(json.dumps(schema.json_schema(), indent=2) + "\n")
    print(f"wrote {path}")


def cmd_verify(args):
    ok, n = default_engine().db.verify()
    print(f"audit chain intact, {n} events" if ok else f"AUDIT CHAIN BROKEN at event {n}")
    return 0 if ok else 1


def cmd_lab(args):
    if args.action == "list":
        active, live = labctl.active_name(), labctl.running()
        for name in lab_names():
            t = Topology.load(name)
            state = "running" if name in live else "stopped"
            print(f"{'*' if name == active else ' '} {name:<18} {t.template:<13} {len(t.names):>2} routers "
                  f"{len(t.links):>2} links  {state}")
        print("\n* = active lab")
    elif args.action == "new":
        t = labgen.new_lab(args.name, args.template, args.routers, args.seed)
        print(f"created labs/{t.name}/lab.yaml: {len(t.names)} routers, {len(t.links)} links. "
              f"Start it with: python agent.py lab deploy {t.name}")
    elif args.action == "deploy":
        labctl.deploy_lab(Topology.load(args.name or labctl.active_name()), log=lambda m: print("  " + m))
        print(f"lab {args.name or labctl.active_name()} is running and active")
    elif args.action == "destroy":
        labctl.destroy(args.name or labctl.active_name())
        print("destroyed")
    elif args.action == "use":
        labctl.set_active(args.name)
        print(f"active lab: {args.name}")
    elif args.action == "show":
        t = Topology.load(args.name or labctl.active_name())
        print(f"{t.name} ({t.template})")
        for r in t.names:
            print(f"  {r}: " + ", ".join(f"{i} {' '.join(map(str, a))}" for i, a in t.interfaces(r).items()))
        for l in t.links:
            print(f"  link {l['a']}:{l['a_if']} {l['a_ip']} ─ {l['b']}:{l['b_if']} {l['b_ip']}  ({l['subnet']})")


def cmd_user(args):
    if args.action == "list":
        for u in auth.users(db_path()):
            print(f"{u['name']:<20} {u['role']}")
        return
    password = getpass.getpass(f"Password for {args.name}: ")
    if password != getpass.getpass("Again: "):
        print("passwords do not match")
        return 1
    try:
        auth.add_user(db_path(), args.name, args.role, password)
    except auth.AuthError as e:
        print(e)
        return 1
    print(f"{args.name} can now sign in as {args.role}")


def cmd_drift(args):
    eng = default_engine()
    d = eng.drift()
    print(d["summary"])
    if d["drift"] and args.action == "accept":
        eng.accept_drift(args.by)
        print(f"accepted as the approved state by {args.by}")
    elif d["drift"] and args.action == "revert":
        return decide(eng, None, args, change=eng.plan_revert(args.by))


def main():
    p = argparse.ArgumentParser(description="PS-008 network management agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    a = sub.add_parser("apply")
    a.add_argument("file")
    a.add_argument("--approver", default=getpass.getuser())
    a.add_argument("--simulate-failure", choices=["deploy", "health"])
    a.add_argument("--failure-router")
    q = sub.add_parser("ask")
    q.add_argument("text", nargs="+")
    q.add_argument("--requested-by", default="operator1")
    q.add_argument("--approver", default=getpass.getuser())
    q.add_argument("--simulate-failure", choices=["deploy", "health"])
    q.add_argument("--failure-router")
    sub.add_parser("history")
    s = sub.add_parser("show")
    s.add_argument("change_id")
    for name in ("reset", "schema", "verify-audit"):
        sub.add_parser(name)
    u = sub.add_parser("user")
    u.add_argument("action", choices=["add", "list"])
    u.add_argument("name", nargs="?")
    u.add_argument("--role", choices=auth.ROLES, default="operator")
    dr = sub.add_parser("drift")
    dr.add_argument("action", nargs="?", choices=["show", "revert", "accept"], default="show")
    dr.add_argument("--by", default=getpass.getuser())
    dr.add_argument("--approver", default="approver1")
    dr.add_argument("--simulate-failure", choices=["deploy", "health"])
    dr.add_argument("--failure-router")
    lab = sub.add_parser("lab")
    lab.add_argument("action", choices=["list", "new", "deploy", "destroy", "use", "show"])
    lab.add_argument("name", nargs="?")
    lab.add_argument("--template", default="ring", choices=list(labgen.TEMPLATES))
    lab.add_argument("--routers", type=int, default=6)
    lab.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    if args.cmd == "lab" and args.action in ("new", "use") and not args.name:
        p.error(f"lab {args.action} needs a lab name")
    if args.cmd == "user" and args.action == "add" and not args.name:
        p.error("user add needs a name")
    handler = {"status": cmd_status, "apply": cmd_apply, "ask": cmd_ask, "history": cmd_history, "show": cmd_show, "reset": cmd_reset,
               "schema": cmd_schema, "verify-audit": cmd_verify, "lab": cmd_lab, "user": cmd_user, "drift": cmd_drift}[args.cmd]
    sys.exit(handler(args) or 0)


if __name__ == "__main__":
    main()
