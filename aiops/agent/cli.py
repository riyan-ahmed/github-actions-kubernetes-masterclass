"""Command line entry point:  python -m aiops.agent <command>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .ingest import manual_alert, parse_webhook
from .runtime import build_runtime
from .graph import resume_incident, run_incident


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aiops.agent", description="SkillPulse AIOps agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="run the webhook server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8085)
    p = sub.add_parser("diagnose", help="diagnose a workload now (evidence is collected live)")
    p.add_argument("--namespace", required=True)
    p.add_argument("--workload", required=True)
    p = sub.add_parser("replay", help="process a saved Grafana webhook payload file")
    p.add_argument("payload")
    p = sub.add_parser("decide", help="approve/reject a pending incident")
    p.add_argument("incident_id")
    p.add_argument("decision", choices=["approve", "reject"])
    p.add_argument("--approver", required=True)
    p = sub.add_parser("verify", help="check recovery of a workload right now")
    p.add_argument("--namespace", required=True)
    p.add_argument("--workload", required=True)
    sub.add_parser("audit-verify", help="verify the audit log hash chain")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    deps, graph = build_runtime(settings)

    if args.cmd == "serve":
        import uvicorn
        from .server import create_app
        print(f"mode={settings.mode} dry_run={settings.dry_run} reasoner={deps.reasoner.name}", flush=True)
        uvicorn.run(create_app(deps, graph), host=args.host, port=args.port)
    elif args.cmd in ("diagnose", "replay"):
        alerts = [manual_alert(args.namespace, args.workload)] if args.cmd == "diagnose" \
            else parse_webhook(json.loads(Path(args.payload).read_text()))
        for a in alerts:
            st = run_incident(graph, a)
            print(f"{st['incident_id']}: {st.get('status')} - {st.get('outcome', 'awaiting approval')}")
            print(f"  report: {st.get('report_path')}")
    elif args.cmd == "decide":
        st = resume_incident(graph, args.incident_id, args.decision, args.approver)
        print(f"{args.incident_id}: {st.get('status')} - {st.get('outcome')}")
    elif args.cmd == "verify":
        from .verify import evaluate_recovery
        res = evaluate_recovery(deps.kube.collect(args.namespace, args.workload, log_tail=0))
        deps.audit.record(None, "manual_recovery_check", "cli", {"namespace": args.namespace, "workload": args.workload, **res})
        print(json.dumps(res, indent=1))
        return 0 if res["recovered"] else 1
    elif args.cmd == "audit-verify":
        ok, msg = deps.audit.verify_chain()
        print(f"audit chain: {msg}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
