#!/usr/bin/env python3
"""Exercise the approval workflow of the agent against a PENDING incident (Remediation Stage A, dry run).

  python3 aiops/scenarios/tools/approval_check.py <incident-id> approve   # negative auth tests, tamper attempt, approve (dry run)
  python3 aiops/scenarios/tools/approval_check.py <incident-id> reject    # reject through scripts/aiops-decide.sh

Scope: skillpulse-stg/backend-sim. Tokens are read into memory from the Secrets and never printed. Every check
prints PASS/FAIL; the exit code is non-zero on any FAIL.
"""
import atexit
import base64
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
NS, DEP = "skillpulse-stg", "backend-sim"
iid, mode = sys.argv[1], sys.argv[2]
ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra else ""), flush=True)


def sh(*a, **k):
    return subprocess.run(a, capture_output=True, text=True, **k)


def secret(ns, name):
    return base64.b64decode(sh("kubectl", "-n", ns, "get", "secret", name, "-o", "jsonpath={.data.token}").stdout).decode()


APPROVAL, WEBHOOK = secret("aiops", "aiops-approval"), secret("aiops", "aiops-webhook")
pf = subprocess.Popen(["kubectl", "-n", "aiops", "port-forward", "svc/aiops-agent", "18097:8085"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
atexit.register(pf.terminate)
time.sleep(4)


def call(path, method="GET", token=None, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:18097{path}", method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def snapshot():
    d = json.loads(sh("kubectl", "-n", NS, "get", "deploy", DEP, "-o", "json").stdout)
    rs = sorted(r["metadata"]["name"] + ":" + str(r["spec"]["replicas"]) for r in json.loads(sh("kubectl", "-n", NS, "get", "rs", "-l", f"app={DEP}", "-o", "json").stdout)["items"])
    return {"spec_sha": hashlib.sha256(json.dumps(d["spec"], sort_keys=True).encode()).hexdigest()[:16], "generation": d["metadata"]["generation"],
            "replicas": d["spec"]["replicas"], "revision": d["metadata"]["annotations"]["deployment.kubernetes.io/revision"], "replicasets": rs}


def agent_incident():
    out = sh("kubectl", "-n", "aiops", "exec", "-i", "deploy/aiops-agent", "--", "python", "-", iid, input="""
import json, sys
from aiops.agent.audit import AuditLog
i = sys.argv[1]
d = json.load(open(f"/data/audit/incidents/{i}.json"))
ev = [{"event": e["event"], "actor": e["actor"], "data": e["data"]} for e in AuditLog("/data/audit").events(i)]
print(json.dumps({"state": d, "events": ev}))
""")
    return json.loads(out.stdout)


code, rep = call(f"/incidents/{iid}/report", token=WEBHOOK)
report = rep.get("report", "")
check("report includes the 'Approval required' section", "## Approval required" in report)
check("report states the action is a server-side dry run", "server-side dry run (nothing will change)" in report)
st = agent_incident()["state"]
check("incident is awaiting approval, nothing executed yet", st["status"] == "awaiting_approval" and st["approval"]["status"] == "pending" and not st.get("execution"))
check("proposal is rollout_undo on skillpulse-stg/backend-sim", st["proposal"]["action_id"] == "rollout_undo" and st["target"]["name"] == DEP and st["target"]["namespace"] == NS)
check("policy decision is requires_approval (not auto, not advisory)", st["policy"]["decision"] == "requires_approval" and st["policy"]["mode"] == "approval" and st["policy"]["dry_run"] is True)
prev = st["evidence"]["metrics"]["previous_revision"]
check("rollback target was proven healthy before approval is even requested", prev["was_ready"] is True, prev["replicaset"])
before = snapshot()
print("   deployment snapshot before:", before, flush=True)

if mode == "approve":
    D = f"/incidents/{iid}/decision"
    ok_body = {"decision": "approve", "approver": "stage-a-tester"}
    check("no token -> 401", call(D, "POST", None, ok_body)[0] == 401)
    check("garbage token -> 401", call(D, "POST", "not-the-token", ok_body)[0] == 401)
    check("the WEBHOOK token cannot approve -> 401", call(D, "POST", WEBHOOK, ok_body)[0] == 401)
    check("approval token + missing approver -> 400", call(D, "POST", APPROVAL, {"decision": "approve"})[0] == 400)
    check("approval token + blank approver -> 400", call(D, "POST", APPROVAL, {"decision": "approve", "approver": "   "})[0] == 400)
    check("invalid decision value -> 400", call(D, "POST", APPROVAL, {"decision": "maybe", "approver": "x"})[0] == 400)
    check("injected command in the decision field -> 400", call(D, "POST", APPROVAL, {"decision": "approve; kubectl delete pvc --all", "approver": "x"})[0] == 400)
    check("malformed incident id -> 400", call("/incidents/..%2f..%2fetc%2fpasswd/decision", "POST", APPROVAL, ok_body)[0] in (400, 404))
    check("well-formed but unknown incident id -> 409", call("/incidents/inc-20200101-000000-aaaaaa/decision", "POST", APPROVAL, ok_body)[0] == 409)
    cur = agent_incident()
    check("after all the rejected requests the incident is STILL pending and nothing ran",
          cur["state"]["approval"]["status"] == "pending" and not [e for e in cur["events"] if e["event"] in ("approval_approved", "approval_rejected", "action_executed")])
    check("deployment untouched by the rejected requests", snapshot() == before)
    tamper = {"decision": "approve", "approver": "stage-a-tester", "action": "delete_pvc", "action_id": "delete_pvc", "namespace": "skillpulse-prd",
              "workload": "mysql", "kind": "StatefulSet", "dry_run": False, "command": "kubectl delete pvc --all"}
    code, resp = call(D, "POST", APPROVAL, tamper)
    check("approval with a valid token and extra tamper fields is accepted but the extra fields are ignored (HTTP 200)", code == 200, str(resp)[:120])
    check("outcome is 'dry-run only: no change made'", resp.get("outcome") == "dry-run only: no change made", str(resp))
    final = agent_incident()
    ev = {e["event"]: e for e in final["events"]}
    seq = [e["event"] for e in final["events"]]
    check("audit order: approval_requested -> approval_approved -> action_executed -> recovery_verification -> incident_closed",
          [x for x in seq if x in ("approval_requested", "approval_approved", "action_executed", "recovery_verification", "incident_closed")] ==
          ["approval_requested", "approval_approved", "action_executed", "recovery_verification", "incident_closed"], str(seq))
    check("approver recorded in the audit log", ev["approval_approved"]["actor"] == "stage-a-tester")
    ex = ev["action_executed"]["data"]
    check("action executed as a DRY RUN against skillpulse-stg/backend-sim only", ex["dry_run"] is True and ex["workload"] == DEP and ex["namespace"] == NS and ex["action"] == "rollout_undo", str(ex)[:160])
    check("rollback target re-checked at execution time (was_ready true)", ex["target_replicaset_ready_history"]["was_ready"] is True)
    check("tamper fields never reached the executor (no prd/mysql/delete_pvc anywhere in the audit trail)", not any(w in json.dumps(final["events"]) for w in ("skillpulse-prd", "mysql", "delete_pvc")))
    check("verification skipped because it was a dry run", final["state"]["verification"]["status"] == "skipped")
    after = snapshot()
    print("   deployment snapshot after: ", after, flush=True)
    check("DEPLOYMENT UNCHANGED by the dry run: same spec, generation, replicas, revision, ReplicaSet scaling", after == before)
    check("incident closed with the right outcome", final["state"]["outcome"] == "dry-run only: no change made" and final["state"]["status"] == "closed")
else:
    r = sh("bash", str(REPO / "scripts/aiops-decide.sh"), "reject", iid, "--approver", "stage-a-tester", "--confirm", iid)
    check("scripts/aiops-decide.sh reject succeeded", r.returncode == 0 and "HTTP 200" in r.stdout, r.stdout[-200:] + r.stderr[-200:])
    check("neither token appears in the script output", APPROVAL not in r.stdout + r.stderr and WEBHOOK not in r.stdout + r.stderr)
    final = agent_incident()
    seq = [e["event"] for e in final["events"]]
    ev = {e["event"]: e for e in final["events"]}
    check("audit shows approval_rejected by the named approver", "approval_rejected" in seq and ev["approval_rejected"]["actor"] == "stage-a-tester", str(seq))
    check("NO action was executed and no verification ran", "action_executed" not in seq and "recovery_verification" not in seq)
    check("outcome: remediation rejected by human", final["state"]["outcome"] == "remediation rejected by human")
    after = snapshot()
    check("DEPLOYMENT UNCHANGED after the rejection", after == before)
    check("a second decision on the closed incident is refused (409)", call(f"/incidents/{iid}/decision", "POST", APPROVAL, {"decision": "approve", "approver": "x"})[0] == 409)
sys.exit(0 if ok else 1)
