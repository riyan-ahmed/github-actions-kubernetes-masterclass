"""Measure one finished GitOps self-healing run. Read-only: it queries the agent API, Prometheus and the Kubernetes API.

  python3 aiops/scenarios/tools/gitops_measure.py <incident-id> --context kind-skillpulse [--inject-file last-inject.json]

Definitions (all in seconds, UTC timestamps):
  failure_onset            first OOMKilled termination (kube-state-metrics last_terminated_timestamp), the real failure
  injection_to_oom         Git push of the regression -> first OOMKilled (Argo CD reconcile + rollout + start-up)
  onset_to_detection       first OOMKilled -> alert received by the agent (Prometheus scrape + Grafana evaluation + webhook)
  detection_to_diagnosis   alert received -> diagnosis recorded (Kubernetes + Prometheus + Loki evidence, rules, runbook)
  diagnosis_to_pr          diagnosis -> pull request created on GitHub
  pr_open_to_human_merge   PR created -> merged by a human (NOT automated; reported separately)
  merge_to_sync_started    merge -> Argo CD sync operation started (Argo CD Git polling interval dominates)
  argocd_sync_duration     Argo CD sync operation started -> finished
  sync_to_healthy          sync finished -> Deployment Available again
  total_incl_human         failure onset -> Deployment Available again
  total_excl_human         total_incl_human minus pr_open_to_human_merge
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote


def kubectl(ctx, *args):
    return subprocess.run(["kubectl", "--context", ctx, *args], capture_output=True, text=True, check=True).stdout


def ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def secs(a, b):
    return None if a is None or b is None else round((b - a).total_seconds(), 1)


def prom(ctx, query, at=None):
    path = ("/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090/proxy/api/v1/query?query="
            + quote(query) + (f"&time={at.timestamp()}" if at else ""))
    return json.loads(kubectl(ctx, "get", "--raw", path))["data"]["result"]


def agent_get(ctx, path):
    """Query the agent through the API server's service proxy (no port-forward, no token printed)."""
    token = subprocess.run(["kubectl", "--context", ctx, "-n", "aiops", "get", "secret", "aiops-webhook", "-o",
                            "jsonpath={.data.token}"], capture_output=True, text=True, check=True).stdout
    import base64
    import urllib.request
    pf = subprocess.Popen(["kubectl", "--context", ctx, "-n", "aiops", "port-forward", "svc/aiops-agent", "18085:8085"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time
        for _ in range(30):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:18085{path}",
                                             headers={"Authorization": "Bearer " + base64.b64decode(token).decode().strip()})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())
            except OSError:
                time.sleep(1)
        raise SystemExit("could not reach the agent")
    finally:
        pf.terminate()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("incident")
    ap.add_argument("--context", default="kind-skillpulse")
    ap.add_argument("--inject-file")
    a = ap.parse_args()

    events = agent_get(a.context, f"/incidents/{a.incident}/events")
    by = {}
    for e in events:
        by.setdefault(e["event"], e)
    need = ["alert_received", "diagnosis", "pr_merged", "argocd_synced", "workload_recovered"]
    missing = [n for n in need if n not in by]
    if missing:
        sys.exit(f"incident {a.incident} is incomplete, missing audit events: {missing}")
    pr = next(e for e in events if e["event"] == "action_executed" and e["data"].get("mode") == "pull_request")
    received = ts(by["alert_received"]["ts"])
    starts = ts(by["alert_received"]["data"]["alert"].get("starts_at"))
    diag = ts(by["diagnosis"]["ts"])
    pr_created = ts(pr["data"]["created_at"])
    merged = ts(by["pr_merged"]["data"]["merged_at"])
    sync_start = ts(by["argocd_synced"]["data"]["operation_started_at"])
    sync_end = ts(by["argocd_synced"]["data"]["operation_finished_at"])

    # first OOMKilled termination, as seen by kube-state-metrics shortly after the alert fired
    oom = None
    res = prom(a.context, 'max(kube_pod_container_status_last_terminated_timestamp{namespace="skillpulse-gitops",pod=~"oom-demo-.*"}'
                          ' and on(namespace,pod,container) (kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1))',
               at=received)
    if res:
        oom = ts(float(res[0]["value"][1]))
    # the Deployment became Available again after the Argo CD sync
    avail = None
    dep = json.loads(kubectl(a.context, "-n", "skillpulse-gitops", "get", "deploy", "oom-demo", "-o", "json"))
    for c in dep["status"].get("conditions", []):
        if c["type"] == "Available" and c["status"] == "True":
            avail = ts(c["lastTransitionTime"])
    recovered_obs = ts(by["workload_recovered"]["data"]["observed_at"])
    injected = None
    if a.inject_file:
        try:
            injected = ts(json.load(open(a.inject_file))["injected_at"])
        except (OSError, KeyError, ValueError):
            pass

    healthy = avail if avail and avail >= sync_start else recovered_obs
    human = secs(pr_created, merged)
    onset = oom or starts
    out = {
        "incident": a.incident, "pr_url": pr["data"]["url"], "merged_by": by["pr_merged"]["data"].get("merged_by"),
        "failure_onset_source": "first OOMKilled (kube-state-metrics)" if oom else "Grafana alert startsAt (fallback)",
        "seconds": {
            "injection_to_oom": secs(injected, oom),
            "onset_to_detection": secs(onset, received),
            "detection_to_diagnosis": secs(received, diag),
            "diagnosis_to_pr": secs(diag, pr_created),
            "pr_open_to_human_merge": human,
            "merge_to_sync_started": secs(merged, sync_start),
            "argocd_sync_duration": secs(sync_start, sync_end),
            "sync_to_healthy": secs(sync_end, healthy),
            "total_incl_human": secs(onset, healthy),
            "total_excl_human": None if human is None or secs(onset, healthy) is None else round(secs(onset, healthy) - human, 1),
        },
        "notes": ["Deployment Available transition used for 'healthy'" if healthy == avail else "tracker observation used for 'healthy' (poll granularity)",
                  "Grafana alert firing at the agent: " + str(secs(starts, received)) + " s after the alert's startsAt"],
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
