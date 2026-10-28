#!/usr/bin/env python3
"""Rehearse the post-upgrade restore of a REAL backup into a clean local Grafana (Docker, no cluster).

  python3 monitoring/validate/grafana_restore_rehearsal.py aiops/audit/grafana-backup-<ts>

Starts Grafana 13.2.4 with the Helm-rendered aiops provisioning (what the upgraded pod will look like),
runs scripts/grafana-alerting-restore.sh twice, and checks that every original rule/group/policy/folder
came back, the aiops objects are untouched, and the second run changed nothing.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import grafana_local_check as g  # noqa: E402

B, PORT, PW, NAME = Path(sys.argv[1]).resolve(), 13011, "check", "gf-restore-rehearsal"
L = lambda n: json.loads((B / n).read_text())  # noqa: E731
tmp, _ = g.build_provisioning()
subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
subprocess.run(["docker", "run", "-d", "--name", NAME, "-p", f"{PORT}:3000", "-e", f"GF_SECURITY_ADMIN_PASSWORD={PW}", "-e", f"AIOPS_WEBHOOK_TOKEN={g.TOKEN}",
                "-v", f"{tmp}/alerting:/etc/grafana/provisioning/alerting:ro", "-v", f"{tmp}/datasources:/etc/grafana/provisioning/datasources:ro", g.IMAGE],
               check=True, capture_output=True)


def api(path):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers={"Authorization": "Basic " + base64.b64encode(f"admin:{PW}".encode()).decode()})
    return json.load(urllib.request.urlopen(r, timeout=20))


ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("[PASS] " if cond else "[FAIL] ") + name)


try:
    for _ in range(60):
        try:
            api("/api/health")
            break
        except Exception:
            time.sleep(1)
    env = {**os.environ, "GRAFANA_URL": f"http://127.0.0.1:{PORT}", "GRAFANA_USER": "admin", "GRAFANA_PASSWORD": PW}
    before = {r["uid"] for r in api("/api/v1/provisioning/alert-rules")}
    r1 = subprocess.run([str(g.ROOT / "scripts/grafana-alerting-restore.sh"), str(B)], env=env, capture_output=True, text=True)
    r2 = subprocess.run([str(g.ROOT / "scripts/grafana-alerting-restore.sh"), str(B)], env=env, capture_output=True, text=True)
    rules = {r["uid"]: r for r in api("/api/v1/provisioning/alert-rules")}
    orig = L("alert-rules.json")
    check("original rules absent in a fresh Grafana (sanity: restore is needed)", not any(r["uid"] in before for r in orig))
    check("restore exit codes 0 and 0 (second run idempotent)", r1.returncode == 0 and r2.returncode == 0)
    check("all original alert rules restored by uid", all(r["uid"] in rules for r in orig))
    check("rule content preserved (title, folder, group, for, receiver, condition)", all(
        all(rules[o["uid"]].get(k) == o.get(k) for k in ("title", "folderUID", "ruleGroup", "for", "condition", "notification_settings", "noDataState", "execErrState")) for o in orig))
    check("rule queries preserved", all(rules[o["uid"]]["data"][0]["model"]["expr"] == o["data"][0]["model"]["expr"] for o in orig))
    for grp in L("rule-groups.json"):
        cur = api(f"/api/v1/provisioning/folder/{grp['folderUid']}/rule-groups/{grp['title']}")
        check(f"rule group {grp['title']} interval = {grp['interval']}s", cur["interval"] == grp["interval"])
    check("original folder restored", {f["uid"] for f in L("folders.json")} <= {f["uid"] for f in api("/api/folders")})
    check("aiops rules untouched (still file-provisioned)", all(rules[u].get("provenance") == "file" for u in ("aiops-crashloop", "aiops-oomkilled", "aiops-pod-not-ready")))
    check("aiops-agent contact point present", any(c["name"] == "aiops-agent" for c in api("/api/v1/provisioning/contact-points")))
    pol = api("/api/v1/provisioning/policies")
    check("notification policy tree equals the backup (default root receiver, no routes)", pol.get("receiver") == L("policies.json").get("receiver") and not pol.get("routes"))
    check("no duplicates after two restores", len(rules) == len(before) + len(orig))
    print(f"[info] rules after restore: {len(rules)} = {len(before)} provisioned by aiops values + {len(orig)} restored")
finally:
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
sys.exit(0 if ok else 1)
