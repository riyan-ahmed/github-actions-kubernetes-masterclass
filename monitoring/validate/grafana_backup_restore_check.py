#!/usr/bin/env python3
"""Prove the Grafana backup/restore scripts against real Grafana 13.2.4 in local Docker.

Simulates exactly what the real upgrade does to the emptyDir-backed Grafana:
  1. Grafana A: UI-style objects (folder, datasource, contact point, rule, policy route, dashboard)
  2. scripts/grafana-alerting-backup.sh
  3. destroy A; start Grafana B from scratch WITH the new aiops provisioning (as after the upgrade)
  4. scripts/grafana-alerting-restore.sh
  5. assert the legacy objects are back, the aiops-* objects are untouched, and nothing was overwritten.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import grafana_local_check as g  # noqa: E402

ROOT = g.ROOT
PORT, NAME, PW = 13002, "aiops-grafana-brcheck", "check"


def api(method, path, body=None, headers=None):
    h = {"Authorization": "Basic " + base64.b64encode(f"admin:{PW}".encode()).decode(), "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, headers=h,
                                 data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def start(extra=()):
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", NAME, "-p", f"{PORT}:3000", "-e", f"GF_SECURITY_ADMIN_PASSWORD={PW}",
                    "-e", f"AIOPS_WEBHOOK_TOKEN={g.TOKEN}", *extra, g.IMAGE], check=True, capture_output=True)
    for _ in range(60):
        try:
            api("GET", "/api/health")
            return
        except Exception:
            time.sleep(1)
    raise SystemExit("grafana did not start")


def main() -> int:
    out = Path(tempfile.mkdtemp(prefix="grafana-backup-"))
    env = {**os.environ, "GRAFANA_URL": f"http://127.0.0.1:{PORT}", "GRAFANA_USER": "admin", "GRAFANA_PASSWORD": PW}
    ok = True
    try:
        start()
        H = {"X-Disable-Provenance": "true"}
        api("POST", "/api/folders", {"uid": "legacy", "title": "Legacy"})
        api("POST", "/api/datasources", {"name": "Prometheus", "uid": "prometheus", "type": "prometheus",
                                         "access": "proxy", "url": "http://nowhere:9090"})
        api("POST", "/api/v1/provisioning/contact-points", {"name": "legacy-email", "type": "email",
                                                            "settings": {"addresses": "ops@example.com"}}, H)
        api("POST", "/api/v1/provisioning/alert-rules", {
            "uid": "legacy-replicas", "title": "Legacy backend replicas", "ruleGroup": "legacy", "folderUID": "legacy",
            "condition": "C", "for": "1m", "noDataState": "OK", "execErrState": "Error", "orgID": 1, "labels": {"team": "legacy"},
            "annotations": {}, "data": [
                {"refId": "A", "relativeTimeRange": {"from": 300, "to": 0}, "datasourceUid": "prometheus",
                 "model": {"refId": "A", "expr": "up", "instant": True}},
                {"refId": "B", "datasourceUid": "__expr__", "model": {"refId": "B", "type": "reduce", "expression": "A", "reducer": "last"}},
                {"refId": "C", "datasourceUid": "__expr__", "model": {"refId": "C", "type": "threshold", "expression": "B",
                                                                       "conditions": [{"evaluator": {"type": "lt", "params": [1]}}]}}]}, H)
        api("PUT", "/api/v1/provisioning/policies", {"receiver": "legacy-email", "group_by": ["alertname"],
                                                      "routes": [{"receiver": "legacy-email", "object_matchers": [["team", "=", "legacy"]]}]}, H)
        api("POST", "/api/dashboards/db", {"dashboard": {"uid": "legacy-dash", "title": "Legacy dashboard", "panels": [], "schemaVersion": 39},
                                           "overwrite": False})
        api("POST", "/api/datasources", {"name": "Legacy Loki", "uid": "legacy-loki", "type": "loki", "access": "proxy", "url": "http://loki:3100"})
        api("POST", "/api/library-elements", {"uid": "legacy-panel", "name": "Legacy panel", "kind": 1, "folderUid": "legacy",
                                              "model": {"type": "text", "title": "Legacy panel", "options": {"content": "hi"}}})
        api("PUT", "/api/v1/provisioning/folder/legacy/rule-groups/legacy", {"title": "legacy", "interval": 300, "folderUid": "legacy",
                                                                             "rules": api("GET", "/api/v1/provisioning/alert-rules/legacy-replicas") and [api("GET", "/api/v1/provisioning/alert-rules/legacy-replicas")]}, H)
        api("POST", "/api/alertmanager/grafana/api/v2/silences", {"matchers": [{"name": "team", "value": "legacy", "isRegex": False, "isEqual": True}],
                                                                  "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5)),
                                                                  "endsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7200)),
                                                                  "createdBy": "ops", "comment": "legacy maintenance"})
        api("PUT", "/api/org/preferences", {"theme": "dark", "timezone": "utc"})
        r = subprocess.run([str(ROOT / "scripts/grafana-alerting-backup.sh"), str(out / "bk")], env=env, capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        ok &= r.returncode == 0

        # --- the "upgrade": brand-new Grafana (empty storage) with the new aiops provisioning
        tmp, _ = g.build_provisioning()
        start(["-v", f"{tmp}/alerting:/etc/grafana/provisioning/alerting:ro", "-v", f"{tmp}/datasources:/etc/grafana/provisioning/datasources:ro"])
        before = {r["uid"] for r in api("GET", "/api/v1/provisioning/alert-rules")}
        assert "legacy-replicas" not in before, "sanity: legacy rule should be gone after the simulated pod recreation"
        r = subprocess.run([str(ROOT / "scripts/grafana-alerting-restore.sh"), str(out / "bk")], env=env, capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        ok &= r.returncode == 0
        # second run must be idempotent (no duplicates, no overwrites)
        r2 = subprocess.run([str(ROOT / "scripts/grafana-alerting-restore.sh"), str(out / "bk")], env=env, capture_output=True, text=True)
        ok &= r2.returncode == 0

        rules = {x["uid"]: x for x in api("GET", "/api/v1/provisioning/alert-rules")}
        cps = {c["name"] for c in api("GET", "/api/v1/provisioning/contact-points")}
        policy = api("GET", "/api/v1/provisioning/policies")
        dashes = {d["uid"] for d in api("GET", "/api/search?type=dash-db")}
        checks = [
            ("legacy alert rule restored", "legacy-replicas" in rules and rules["legacy-replicas"]["labels"] == {"team": "legacy"}),
            ("legacy contact point restored", "legacy-email" in cps),
            ("legacy notification route restored", any(rt.get("receiver") == "legacy-email" for rt in policy.get("routes", []))),
            ("legacy dashboard restored", "legacy-dash" in dashes),
            ("aiops rules still present and unmodified", {"aiops-crashloop", "aiops-oomkilled", "aiops-pod-not-ready"} <= set(rules)
             and all(rules[u].get("provenance") == "file" for u in ("aiops-crashloop", "aiops-oomkilled", "aiops-pod-not-ready"))),
            ("aiops-agent contact point intact", "aiops-agent" in cps),
            ("restore is idempotent (no duplicate rules)", len([u for u in rules if u == "legacy-replicas"]) == 1
             and len(api("GET", "/api/v1/provisioning/alert-rules")) == len(rules)),
            ("UI datasource restored (without secrets)", "legacy-loki" in {d["uid"] for d in api("GET", "/api/datasources")}),
            ("library panel restored", "legacy-panel" in {e["uid"] for e in api("GET", "/api/library-elements?perPage=500")["result"]["elements"]}),
            ("rule-group interval (300s) restored", api("GET", "/api/v1/provisioning/folder/legacy/rule-groups/legacy")["interval"] == 300),
            ("active silence restored", any(sl["comment"].startswith("legacy maintenance") and sl["status"]["state"] != "expired"
                                            for sl in api("GET", "/api/alertmanager/grafana/api/v2/silences"))),
            ("org preferences restored", api("GET", "/api/org/preferences").get("theme") == "dark"),
            ("restored objects stay editable (not file-provenance)", rules["legacy-replicas"].get("provenance", "") == ""),
        ]
        for name, passed in checks:
            print(("[PASS] " if passed else "[FAIL] ") + name)
            ok &= bool(passed)
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
