"""Terminal front end for the agent (also the demo fallback if the dashboard breaks).

  python agent.py status                      routers, routes, who reaches which LAN
  python agent.py apply intents/1-add-route.json [--simulate-failure deploy|health] [--approver NAME]
  python agent.py history                     every change and its status
  python agent.py show <change_id>            one change with its full audit trail
  python agent.py reset                       restore all routers to lab/configs baseline
  python agent.py schema                      write docs/intent-schema.json
  python agent.py verify-audit                check the audit hash chain
"""
import argparse
import getpass
import json
import select
import sys
import time

from backend import net, schema
from backend.engine import default_engine

COLOR = {"PASS": "\033[32m", "SUCCESS": "\033[32m", "+": "\033[32m", "-": "\033[31m", "FAIL": "\033[31m",
         "INVALID": "\033[31m", "BLOCKED": "\033[31m", "REJECTED": "\033[33m", "ROLLED_BACK": "\033[33m",
         "ROLLBACK_FAILED": "\033[41m", "PENDING_APPROVAL": "\033[36m"}
END = "\033[0m"


def paint(text, key):
    return f"{COLOR.get(key, '')}{text}{END}" if sys.stdout.isatty() else text


def print_change(c):
    print(f"\nChange {c['id']}  status: {paint(c['status'], c['status'])}")
    print(f"  {c['reason']}")
    for r in c.get("report", []):
        result = "PASS" if r["ok"] else "FAIL"
        print(f"  [{paint(result, result)}] {r['check']:<12} {r['detail']}")
    for router, lines in c.get("diff", {}).items():
        print(f"  {router} static routes:")
        for line in lines:
            print("    " + paint(line, line[0]))


def cmd_status(eng, _):
    n = eng.network()
    for r, info in n["routers"].items():
        print(f"{r} ({info['node']})  connected: {', '.join(info['connected'])}")
        for prefix, nh in info["static"]:
            print(f"    ip route {prefix} {nh}")
    print("\nReachability (twin model of the live routes):")
    for x in n["reachability"]:
        print(f"  {x['src']} → {x['dst']:<16} {paint(x['outcome'], 'PASS' if x['outcome'] == 'DELIVERED' else 'FAIL')}"
              f"  {' → '.join(x['path'])}")


def cmd_apply(eng, args):
    raw = json.loads(open(args.file).read())
    c = eng.plan(raw)
    print_change(c)
    if c["status"] != "PENDING_APPROVAL":
        return 1
    left = int(c["expires_at"] - time.time())
    print(f"\nApprove and deploy? Type 'yes' to approve, anything else rejects. Auto-reject in {left}s: ", end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], left)
    if not ready:
        print()
        c = eng.get(c["id"])  # expiry turns it into REJECTED
    else:
        answer = sys.stdin.readline().strip().lower()
        print("Reason: ", end="", flush=True)
        reason = sys.stdin.readline().strip()
        if answer == "yes":
            print("Backing up, deploying, checking health...")
            c = eng.apply(c["id"], args.approver, reason, args.simulate_failure)
        else:
            c = eng.reject(c["id"], args.approver, reason)
    print(f"\nResult: {paint(c['status'], c['status'])}\n  {c['reason']}")
    return 0 if c["status"] in ("SUCCESS", "REJECTED") else 1


def cmd_history(eng, _):
    for c in eng.list():
        i = c.get("normalized") or c["intent"]
        what = f"{i.get('intent')} {i.get('router')} {i.get('destination')} {i.get('next_hop', '')}" if isinstance(i, dict) else str(i)
        when = time.strftime("%H:%M:%S", time.localtime(c["created_at"]))
        print(f"{c['id']}  {when}  {paint(f'{c['status']:<16}', c['status'])} {what}")


def cmd_show(eng, args):
    c = eng.get(args.change_id)
    if not c:
        print("no such change")
        return 1
    print_change(c)
    print("\nAudit trail:")
    for e in eng.db.events(c["id"]):
        print(f"  {e['ts']}  {e['stage']:<18} {e['result']:<10} {e['detail'][:120]}")


def cmd_reset(eng, _):
    for r, (ok, out) in eng.reset().items():
        print(f"{r}: {'restored to baseline' if ok else 'FAILED ' + out}")


def cmd_schema(_eng, _):
    path = net.ROOT / "docs" / "intent-schema.json"
    path.write_text(json.dumps(schema.json_schema(), indent=2) + "\n")
    print(f"wrote {path}")


def cmd_verify(eng, _):
    ok, n = eng.db.verify()
    print(f"audit chain intact, {n} events" if ok else f"AUDIT CHAIN BROKEN at event {n}")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="PS-008 network management agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    a = sub.add_parser("apply")
    a.add_argument("file")
    a.add_argument("--approver", default=getpass.getuser())
    a.add_argument("--simulate-failure", choices=["deploy", "health"])
    sub.add_parser("history")
    s = sub.add_parser("show")
    s.add_argument("change_id")
    sub.add_parser("reset")
    sub.add_parser("schema")
    sub.add_parser("verify-audit")
    args = p.parse_args()
    handler = {"status": cmd_status, "apply": cmd_apply, "history": cmd_history, "show": cmd_show,
               "reset": cmd_reset, "schema": cmd_schema, "verify-audit": cmd_verify}[args.cmd]
    sys.exit(handler(default_engine(), args) or 0)


if __name__ == "__main__":
    main()
