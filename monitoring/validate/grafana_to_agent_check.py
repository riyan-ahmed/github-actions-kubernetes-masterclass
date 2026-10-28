#!/usr/bin/env python3
"""Grafana -> real agent image, over a Docker network (no cluster involved, no manual POST).

Real Grafana 13.2.4 is provisioned with the exact rules + contact point rendered by the Helm values
(only the Prometheus URL and the webhook URL host are rewritten). A mock Prometheus serves a
crash-looping backend-sim series. The agent container is the production image, started the way
Kubernetes will (non-root, read-only rootfs, no capabilities) with the webhook token from env.
Asserts that Grafana's own evaluation produced authenticated deliveries the agent accepted.
Kubernetes is unreachable here (fake kubeconfig), so the incident is 'ignored: workload not found':
this proves detection -> delivery -> authentication -> ingestion, not evidence collection.
Usage: python3 monitoring/validate/grafana_to_agent_check.py [image] [--timeout 420]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import grafana_local_check as g  # noqa: E402

image = next((a for a in sys.argv[1:] if not a.startswith("--")), "skillpulse-aiops-agent:0.4.0")
timeout = int(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 420
NET, AGENT, GRAF = "aiops-g2a", "aiops-agent-check", "aiops-grafana-g2a"
TOKEN = "g2a-test-token-0123456789abcdef"
ok = True


def sh(*a, **k):
    return subprocess.run(a, capture_output=True, text=True, **k)


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("[PASS] " if cond else "[FAIL] ") + name + (f"  [{extra}]" if extra and not cond else ""), flush=True)


tmp, _ = g.build_provisioning()
cp_file = tmp / "alerting/contactpoints.yaml"
cp = yaml.safe_load(cp_file.read_text())
s = cp["contactPoints"][0]["receivers"][0]["settings"]
s["url"] = f"http://{AGENT}:8085/webhook/grafana"     # same path/port/scheme as the in-cluster Service URL
cp_file.write_text(yaml.safe_dump(cp))
rules = yaml.safe_load((tmp / "alerting/rules.yaml").read_text())
# serve a backend-sim crash-loop series from the mock Prometheus
g.Prom._reply = lambda self: (self.send_response(200), self.send_header("Content-Type", "application/json"), self.end_headers(),
    self.wfile.write(json.dumps({"status": "success", "data": {"resultType": "vector", "result": [{"metric": {
        "namespace": "skillpulse-stg", "pod": "backend-sim-556595f94d-w9dpf", "container": "backend"}, "value": [time.time(), "1"]}]}}).encode()))
prom = g.serve(g.Prom, g.PROM_PORT)
(tmp / "kubeconfig").write_text("apiVersion: v1\nkind: Config\nclusters:\n- name: x\n  cluster: {server: 'https://127.0.0.1:1'}\nusers:\n- name: x\n  user: {token: fake}\n"
                                 "contexts:\n- name: x\n  context: {cluster: x, user: x}\ncurrent-context: x\n")
for n in (AGENT, GRAF):
    sh("docker", "rm", "-f", n)
sh("docker", "network", "rm", NET)
sh("docker", "network", "create", NET)
try:
    r = sh("docker", "run", "-d", "--name", AGENT, "--network", NET, "--platform", "linux/arm64", "--user", "10001:10001", "--read-only",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp", "--tmpfs", "/data:rw,mode=1777",
           "-e", "AIOPS_MODE=diagnose", "-e", "AIOPS_DRY_RUN=true", "-e", "AIOPS_AUDIT_DIR=/data/audit", "-e", f"AIOPS_WEBHOOK_TOKEN={TOKEN}",
           "-e", "AIOPS_APPROVAL_TOKEN=unused-approval-token-xyz", "-e", "KUBECONFIG=/kube/config", "-v", f"{tmp / 'kubeconfig'}:/kube/config:ro", image)
    check("agent container started (read-only rootfs, no caps)", r.returncode == 0, r.stderr[-200:])
    r = sh("docker", "run", "-d", "--name", GRAF, "--network", NET, "-p", f"{g.GRAFANA_PORT}:3000", "-e", "GF_SECURITY_ADMIN_PASSWORD=check",
           "-e", f"AIOPS_WEBHOOK_TOKEN={TOKEN}", "-v", f"{tmp}/alerting:/etc/grafana/provisioning/alerting:ro",
           "-v", f"{tmp}/datasources:/etc/grafana/provisioning/datasources:ro", g.IMAGE)
    check("grafana container started with the Helm-rendered provisioning", r.returncode == 0, r.stderr[-200:])
    for _ in range(60):
        try:
            g.grafana("/api/health")
            break
        except Exception:
            time.sleep(1)
    print(f"[info] waiting up to {timeout}s for Grafana to evaluate (1 m interval; for: 1 m / 2 m) and deliver ...", flush=True)

    def audit():
        out = sh("docker", "exec", AGENT, "python", "-c",
                 "import json; from aiops.agent.audit import AuditLog; l=AuditLog('/data/audit'); print(json.dumps([(e['ts'],e['event'],e['data']) for e in l.events()])); print(l.verify_chain()[0])")
        lines = out.stdout.strip().splitlines()
        return (json.loads(lines[0]), lines[1] == "True") if len(lines) >= 2 else ([], False)

    seen = {}
    t0 = time.time()
    while time.time() - t0 < timeout:
        ev, _ = audit()
        for ts, e, d in ev:
            if e == "alert_received":
                seen.setdefault(d["alert"]["name"], (ts, "new incident"))
            if e == "alert_deduplicated":
                seen.setdefault(d["alert"], (ts, "deduplicated into the open incident"))
        if len({"SkillPulse CrashLoopBackOff", "SkillPulse OOMKilled", "SkillPulse Pod Running But Not Ready"} & set(seen)) == 3:
            break
        time.sleep(5)
    elapsed = time.time() - t0
    ev, chain_ok = audit()
    print(f"[info] alerts the agent ingested after {elapsed:.0f}s: " + json.dumps({k: v[1] for k, v in seen.items()}), flush=True)
    check("Grafana delivered the CrashLoopBackOff alert to the agent without any manual POST", "SkillPulse CrashLoopBackOff" in seen)
    check("authenticated: agent accepted Grafana's Bearer token (it returns 401 otherwise)", bool(seen))
    check("all three aiops rules reached the agent (crash-loop, OOM, not-ready)", len(seen) >= 3, str(list(seen)))
    check("duplicate alerts for one workload were deduplicated, not turned into extra incidents",
          sum(1 for _, e, _ in ev if e == "alert_received") == 1, str([e for _, e, _ in ev]))
    check("audit hash chain verifies in the agent", chain_ok)
    logs = sh("docker", "logs", AGENT)
    check("agent logs show 200/202 for the webhook and no 401", "401" not in logs.stdout + logs.stderr and "202" in logs.stdout + logs.stderr, (logs.stdout + logs.stderr)[-300:])
    check("token not in agent logs", TOKEN not in logs.stdout + logs.stderr)
    gl = sh("docker", "logs", GRAF)
    check("grafana logs show no notification errors for aiops-agent", "Notify for alerts failed" not in gl.stdout + gl.stderr, (gl.stdout + gl.stderr)[-300:])
finally:
    for n in (AGENT, GRAF):
        sh("docker", "rm", "-f", n)
    sh("docker", "network", "rm", NET)
    prom.shutdown()
sys.exit(0 if ok else 1)
