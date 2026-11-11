#!/usr/bin/env python3
"""Local container check for a candidate image (no cluster involved).

  python3 aiops/docker/container_check.py aiops-agent-cand:chainguard [/path/to/python-in-image]

1. runs smoke_test.py inside the image with the image's own Python,
2. starts the server the way Kubernetes will: non-root, --read-only root fs, no capabilities,
   no-new-privileges, tmpfs for /data and /tmp, tokens only via env,
3. checks /healthz, authentication (401/503), a valid-token webhook, audit + report files, hash chain.
A fake kubeconfig pointing at an unreachable address is used, so Kubernetes lookups fail
(incidents are 'ignored'): this verifies packaging and HTTP/auth/audit, not live evidence collection.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

image = sys.argv[1]
HERE = Path(__file__).resolve().parent
WEB, APPR = "test-webhook-token-0123456789", "test-approval-token-9876543210"
ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def sh(*a, **k):
    return subprocess.run(a, capture_output=True, text=True, **k)


entry = sh("docker", "image", "inspect", image, "--format", "{{json .Config.Entrypoint}}").stdout.strip()
py = json.loads(entry)[0]
user = sh("docker", "image", "inspect", image, "--format", "{{.Config.User}}").stdout.strip()
print(f"image={image} entrypoint-python={py} user={user!r}")
check("image runs as a non-root user", user not in ("", "0", "root", "0:0"), user)

RUN_AS = os.environ.get("CHECK_USER")        # e.g. CHECK_USER=10001:10001 to mimic the pod securityContext
hardening = (["--user", RUN_AS] if RUN_AS else []) + ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp"]
env_paths = sh("docker", "image", "inspect", image, "--format", "{{range .Config.Env}}{{println .}}{{end}}").stdout
pp = [] if "PYTHONPATH=" in env_paths else ["-e", "PYTHONPATH=/app"]   # script dir, not cwd, is sys.path[0]
r = sh("docker", "run", "--rm", *hardening, "--tmpfs", "/data:rw,mode=1777", "--platform", "linux/arm64", "--entrypoint", py,
       *pp, "-e", "AIOPS_AUDIT_DIR=/data/audit", "-v", f"{HERE / 'smoke_test.py'}:/smoke_test.py:ro", image, "/smoke_test.py")
print(r.stdout.rstrip() or r.stderr[-1500:])
check("in-image smoke test (policy, dangerous actions, audit, redaction, retrieval, sqlite)", r.returncode == 0, r.stderr[-300:])

tmp = Path(tempfile.mkdtemp(prefix="aiops-container-"))
(tmp / "kubeconfig").write_text(
    "apiVersion: v1\nkind: Config\nclusters:\n- name: x\n  cluster: {server: 'https://127.0.0.1:1'}\n"
    "users:\n- name: x\n  user: {token: fake}\ncontexts:\n- name: x\n  context: {cluster: x, user: x}\ncurrent-context: x\n")
name = "aiops-cand-check"
sh("docker", "rm", "-f", name)
run = sh("docker", "run", "-d", "--name", name, "--platform", "linux/arm64", *hardening, "--tmpfs", "/data:rw,mode=1777",
         "-p", "18095:8085", "-e", "AIOPS_MODE=diagnose", "-e", "AIOPS_DRY_RUN=true", "-e", "AIOPS_AUDIT_DIR=/data/audit",
         "-e", f"AIOPS_WEBHOOK_TOKEN={WEB}", "-e", f"AIOPS_APPROVAL_TOKEN={APPR}", "-e", "KUBECONFIG=/kube/config",
         "-v", f"{tmp / 'kubeconfig'}:/kube/config:ro", image)
check("container starts with read-only rootfs, no caps, no-new-privileges", run.returncode == 0, run.stderr[-300:])


def call(path, method="GET", token=None, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:18095{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


try:
    for _ in range(40):
        try:
            code, health = call("/healthz")
            break
        except Exception:
            time.sleep(1)
    else:
        print(sh("docker", "logs", name).stdout[-1500:], sh("docker", "logs", name).stderr[-1500:])
        raise SystemExit("server did not start")
    check("/healthz ok, mode=diagnose, dry_run=true", code == 200 and health["mode"] == "diagnose" and health["dry_run"] is True, str(health))
    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "x", "namespace": "kube-system", "deployment": "coredns"}}]}
    check("no token -> 401", call("/webhook/grafana", "POST", None, payload)[0] == 401)
    check("wrong token -> 401", call("/webhook/grafana", "POST", "nope", payload)[0] == 401)
    check("approval endpoint rejects the webhook token", call("/incidents/inc-x/decision", "POST", WEB, {"decision": "approve", "approver": "a"})[0] == 401)
    check("webhook rejects the approval token", call("/webhook/grafana", "POST", APPR, payload)[0] == 401)
    code, resp = call("/webhook/grafana", "POST", WEB, payload)
    check("valid token accepted (202)", code == 202 and resp.get("accepted"))
    iid = resp["accepted"][0]
    for _ in range(20):
        code, rep = call(f"/incidents/{iid}/report", "GET", WEB)
        if code == 200:
            break
        time.sleep(1)
    check("report generated and readable", code == 200 and "not monitored" in rep.get("report", "").lower() or "ignored" in rep.get("report", "").lower(), str(rep)[:200])
    code, resp = call("/webhook/grafana", "POST", WEB, {"alerts": [{"status": "firing", "labels": {"alertname": "x", "namespace": "skillpulse-stg", "deployment": "backend"}}]})
    time.sleep(3)
    code2, rep2 = call(f"/incidents/{resp['accepted'][0]}/report", "GET", WEB)
    check("monitored namespace with unreachable Kubernetes degrades safely (no crash, incident closed)", code2 == 200)
    check("server still healthy after both incidents", call("/healthz")[0] == 200)
    audit = sh("docker", "exec", name, py, "-c",
               "from aiops.agent.audit import AuditLog; l=AuditLog('/data/audit'); print(l.verify_chain()[0], len(l.events()))")
    check("audit chain verifies in the running container", audit.stdout.split()[0:1] == ["True"], audit.stdout + audit.stderr[-200:])
    logs = sh("docker", "logs", name)
    check("container logs contain no token values", WEB not in logs.stdout + logs.stderr and APPR not in logs.stdout + logs.stderr)
    write = sh("docker", "exec", name, py, "-c", "open('/app/x','w')")
    check("root filesystem is read-only", write.returncode != 0 and "Read-only" in write.stderr, write.stderr[-120:])
finally:
    sh("docker", "rm", "-f", name)
sys.exit(0 if ok else 1)
