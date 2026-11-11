#!/usr/bin/env python3
"""Offline validation of monitoring/aiops-grafana-values.yaml against a REAL Grafana in local Docker.

No cluster is touched. The script:
  1. renders the kube-prometheus-stack chart with the same -f files used for the real upgrade,
  2. extracts the Grafana provisioning files from the rendered ConfigMaps,
  3. starts grafana/grafana:<version> in Docker with those files, a mock Prometheus
     (always returns one series) and a mock webhook receiver (records the request),
  4. waits for Grafana to evaluate the provisioned rules and deliver the webhook,
  5. asserts rules/contact point were provisioned, routing bypassed the notification policy,
     and the Authorization header carries the Bearer token read from $__env{}.

Only two values are rewritten for the local run: the Prometheus datasource URL and the webhook URL.
Usage: python3 monitoring/validate/grafana_local_check.py [--save-payload PATH] [--keep]
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE, NAME = "grafana/grafana:13.2.4", "aiops-grafana-check"
PROM_PORT, HOOK_PORT, GRAFANA_PORT = 19090, 18085, 13000
TOKEN = "local-test-token"
received: list[dict] = []


class Prom(BaseHTTPRequestHandler):
    def _reply(self):
        body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": [{
            "metric": {"namespace": "skillpulse-stg", "pod": "backend-sim-6d9f7c8b5-x2k4q", "container": "backend"},
            "value": [time.time(), "1"]}]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._reply()

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._reply()

    def log_message(self, *a):
        pass


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        received.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(raw)})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def serve(handler, port):
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def grafana(path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{GRAFANA_PORT}{path}", method=method, headers={
        "Authorization": "Basic " + base64.b64encode(b"admin:check").decode()})
    return json.load(urllib.request.urlopen(req, timeout=10))


def build_provisioning() -> tuple[Path, str]:
    """Render the chart and write Grafana provisioning files (with local URLs) into a temp dir."""
    rendered = subprocess.run(
        ["helm", "template", "monitoring", "prometheus-community/kube-prometheus-stack", "--version", "92.13.1",
         "-n", "monitoring", "-f", str(ROOT / "monitoring/values.yaml"), "-f", str(ROOT / "monitoring/aiops-grafana-values.yaml")],
        check=True, capture_output=True, text=True).stdout
    cms = {d["metadata"]["name"]: d for d in yaml.safe_load_all(rendered) if d and d.get("kind") == "ConfigMap"}
    graf = cms["monitoring-grafana"]["data"]
    ds_cm = cms["monitoring-kube-prometheus-grafana-datasource"]["data"]["datasource.yaml"]

    tmp = Path(tempfile.mkdtemp(prefix="aiops-grafana-"))
    (tmp / "alerting").mkdir()
    (tmp / "datasources").mkdir()
    cp = yaml.safe_load(graf["contactpoints.yaml"])
    url = cp["contactPoints"][0]["receivers"][0]["settings"]["url"]
    cp["contactPoints"][0]["receivers"][0]["settings"]["url"] = f"http://host.docker.internal:{HOOK_PORT}/webhook/grafana"
    (tmp / "alerting/contactpoints.yaml").write_text(yaml.safe_dump(cp))
    (tmp / "alerting/rules.yaml").write_text(graf["rules.yaml"])
    ds = yaml.safe_load(ds_cm)
    ds["datasources"] = [d for d in ds["datasources"] if d["uid"] != "alertmanager"]
    for d in ds["datasources"]:
        if d["uid"] == "prometheus":
            d["url"] = f"http://host.docker.internal:{PROM_PORT}/"
    (tmp / "datasources/ds.yaml").write_text(yaml.safe_dump(ds))
    return tmp, url



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save-payload")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args()

    tmp, url = build_provisioning()
    print(f"[info] real webhook URL in chart values: {url}")

    p1, p2 = serve(Prom, PROM_PORT), serve(Hook, HOOK_PORT)
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", NAME, "-p", f"{GRAFANA_PORT}:3000",
                    "-e", "GF_SECURITY_ADMIN_PASSWORD=check", "-e", f"AIOPS_WEBHOOK_TOKEN={TOKEN}",
                    "-v", f"{tmp}/alerting:/etc/grafana/provisioning/alerting:ro",
                    "-v", f"{tmp}/datasources:/etc/grafana/provisioning/datasources:ro", IMAGE],
                   check=True, capture_output=True)
    ok = True
    try:
        for _ in range(60):
            try:
                grafana("/api/health")
                break
            except Exception:
                time.sleep(1)
        else:
            print(subprocess.run(["docker", "logs", NAME], capture_output=True, text=True).stdout[-2000:])
            raise SystemExit("[FAIL] Grafana did not start (provisioning error?)")

        rules = grafana("/api/v1/provisioning/alert-rules")
        by_uid = {r["uid"]: r for r in rules}
        points = grafana("/api/v1/provisioning/contact-points")
        checks = [
            ("3 aiops rules provisioned", {"aiops-crashloop", "aiops-oomkilled", "aiops-pod-not-ready"} <= set(by_uid)),
            ("rules routed via notification_settings",
             all(by_uid[u].get("notification_settings", {}).get("receiver") == "aiops-agent" for u in by_uid)),
            ("contact point aiops-agent (webhook)", any(p["name"] == "aiops-agent" and p["type"] == "webhook" for p in points)),
            ("token not stored in plaintext in API", all("local-test-token" not in json.dumps(p) for p in points)),
        ]
        policy = grafana("/api/v1/provisioning/policies")
        checks.append(("root notification policy untouched (default receiver, no aiops route)",
                       "aiops" not in json.dumps(policy.get("routes", [])) and policy.get("receiver") != "aiops-agent"))
        print("[info] waiting for Grafana to evaluate rules and call the webhook (up to %ss)..." % args.timeout)
        end = time.time() + args.timeout
        while time.time() < end and not received:
            time.sleep(5)
        checks.append(("webhook delivered by Grafana", bool(received)))
        if received:
            r = received[-1]
            checks.append(("Authorization header is 'Bearer <token from $__env>'", r["auth"] == f"Bearer {TOKEN}"))
            checks.append(("payload has alerts with namespace/pod labels",
                           all("namespace" in a["labels"] and "pod" in a["labels"] for a in r["body"]["alerts"])))
            if args.save_payload:
                Path(args.save_payload).write_text(json.dumps(r["body"], indent=1))
                print(f"[info] saved real Grafana payload to {args.save_payload}")
            print("[info] alert names received:", sorted({a["labels"]["alertname"] for x in received for a in x["body"]["alerts"]}))
        for name, passed in checks:
            print(("[PASS] " if passed else "[FAIL] ") + name)
            ok &= bool(passed)
    finally:
        if not args.keep:
            subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
        p1.shutdown()
        p2.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
