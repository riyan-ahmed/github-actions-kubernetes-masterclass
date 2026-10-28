#!/usr/bin/env python3
"""Inject a backend-sim fault, then only OBSERVE until Grafana has delivered an alert to the agent.

  python3 aiops/scenarios/tools/observe.py <crashloop|oom|readiness> [--out FILE] [--max-wait 720]

Read-only after the injection: it performs GET requests only and never POSTs an alert. It timestamps the first
moment each signal appears (Kubernetes, Prometheus, Grafana alert state, the agent's incident list) and saves
the agent's incident report. Tokens are read into memory from the cluster Secrets and never printed.
Scope: namespace skillpulse-stg, Deployment backend-sim, manifests under aiops/scenarios only.
"""
import argparse
import atexit
import base64
import json
import pathlib
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
NS = "skillpulse-stg"
SCENARIOS = {  # manifest, Grafana rules expected to fire
    "crashloop": ("crashloop/backend-sim-v2-invalid-port.yaml", ["SkillPulse CrashLoopBackOff"]),
    "oom": ("oom/backend-sim-v2-oom.yaml", ["SkillPulse OOMKilled"]),
    "readiness": ("readiness/backend-sim-v2-bad-probe.yaml", ["SkillPulse Pod Running But Not Ready"]),
}
ap = argparse.ArgumentParser()
ap.add_argument("scenario", choices=SCENARIOS)
ap.add_argument("--out")
ap.add_argument("--max-wait", type=int, default=720)
args = ap.parse_args()
MANIFEST, EXPECTED = SCENARIOS[args.scenario]
OUT = pathlib.Path(args.out or REPO / "aiops" / "audit" / "scenarios" / f"{args.scenario}-{time.strftime('%Y%m%d-%H%M%S')}.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

ev, now = {}, time.time
iso = lambda t: time.strftime("%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


def mark(name, **kw):
    if name not in ev:
        ev[name] = {"t": now(), **kw}
        print(f"{iso(ev[name]['t'])}  +{ev[name]['t'] - ev['t_inject']['t']:6.1f}s  {name} {kw or ''}", flush=True)


def secret(ns, name, key):
    return base64.b64decode(sh("kubectl", "-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}").stdout).decode()


def pods():
    return json.loads(sh("kubectl", "-n", NS, "get", "pods", "-l", "app=backend-sim", "-o", "json").stdout)["items"]


# ---- safety: baseline must be healthy before we break anything ----
dep = json.loads(sh("kubectl", "-n", NS, "get", "deploy", "backend-sim", "-o", "json").stdout)
if (dep["status"].get("readyReplicas") or 0) != 1 or dep["spec"]["replicas"] != 1:
    sys.exit("refusing to inject: backend-sim is not at a healthy 1/1 baseline")
baseline_pods = {p["metadata"]["name"] for p in pods()}
WH, GU, GP = secret("aiops", "aiops-webhook", "token"), secret("monitoring", "monitoring-grafana", "admin-user"), secret("monitoring", "monitoring-grafana", "admin-password")
pfs = [subprocess.Popen(["kubectl", "-n", n, "port-forward", f"svc/{s}", pm], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
       for n, s, pm in (("monitoring", "monitoring-grafana", "13011:80"), ("aiops", "aiops-agent", "18095:8085"),
                        ("monitoring", "monitoring-kube-prometheus-prometheus", "19090:9090"))]
atexit.register(lambda: [p.terminate() for p in pfs])
time.sleep(4)


def http(url, hdr=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr or {}), timeout=6) as r:
            return json.load(r)
    except Exception:
        return None


ga = {"Authorization": "Basic " + base64.b64encode(f"{GU}:{GP}".encode()).decode()}
aa = {"Authorization": f"Bearer {WH}"}
prom = lambda q: (http("http://localhost:19090/api/v1/query?" + urllib.parse.urlencode({"query": q})) or {}).get("data", {}).get("result")
base_incidents = set(http("http://localhost:18095/incidents", aa) or [])


def incident_age_s(iid):
    """Incident ids look like inc-YYYYMMDD-HHMMSS-xxxxxx (UTC)."""
    t = time.strptime(iid[4:19], "%Y%m%d-%H%M%S")
    return time.time() - (time.mktime(t) - time.timezone)


DEDUPE_S = 600   # the agent merges alerts for one workload into an open incident for 10 minutes
def is_firing(iid):
    rep = (http(f"http://localhost:18095/incidents/{iid}/report", aa) or {}).get("report", "")
    return "(firing," in rep.split("\n- **Affected")[0]


recent = [i for i in base_incidents if incident_age_s(i) < DEDUPE_S + 30 and is_firing(i)]
if recent:
    wait = int(DEDUPE_S + 30 - min(incident_age_s(i) for i in recent))
    print(f"waiting {wait}s so the previous incident leaves the agent's {DEDUPE_S}s dedupe window and its Grafana alert group settles ...", flush=True)
    time.sleep(max(wait, 0))
    base_incidents = set(http("http://localhost:18095/incidents", aa) or [])
print(f"scenario={args.scenario} baseline_pods={sorted(baseline_pods)} baseline_incidents={len(base_incidents)}  (this tool never POSTs)", flush=True)

ev["t_inject"] = {"t": now()}
print(f"{iso(ev['t_inject']['t'])}  +   0.0s  t_inject: kubectl apply {MANIFEST}", flush=True)
sh("kubectl", "apply", "-f", str(HERE / MANIFEST))
mark("apply_returned")

inc, alerting, new_pods, judged = None, set(), set(), set()
SIGNALS = {"crashloop": 'kube_pod_container_status_waiting_reason{namespace="skillpulse-stg",pod=~"backend-sim-.*",reason="CrashLoopBackOff"} == 1',
           "oom": 'kube_pod_container_status_last_terminated_reason{namespace="skillpulse-stg",pod=~"backend-sim-.*",reason="OOMKilled"} == 1',
           "readiness": 'kube_pod_status_ready{namespace="skillpulse-stg",pod=~"backend-sim-.*",condition="false"} == 1'}
while now() - ev["t_inject"]["t"] < args.max_wait:
    for p in pods():
        name = p["metadata"]["name"]
        if name in baseline_pods:
            continue
        mark("k8s_new_pod_created", pod=name)
        new_pods.add(name)
        cs = (p["status"].get("containerStatuses") or [{}])[0]
        lt = (cs.get("lastState") or {}).get("terminated")
        if lt:
            mark("k8s_first_termination", reason=lt.get("reason"), exit_code=lt.get("exitCode"))
        w = (cs.get("state") or {}).get("waiting") or {}
        if w.get("reason") == "CrashLoopBackOff":
            mark("k8s_status_CrashLoopBackOff", restarts=cs.get("restartCount"))
        if (cs.get("state") or {}).get("running") and not cs.get("ready"):
            mark("k8s_running_but_not_ready", restarts=cs.get("restartCount"))
    if prom(SIGNALS[args.scenario]):
        mark("prometheus_sees_signal")
    for g in ((http("http://localhost:13011/api/prometheus/grafana/api/v1/rules", ga) or {}).get("data") or {}).get("groups", []):
        for r in g["rules"]:
            for a in r.get("alerts") or []:
                if a["labels"].get("pod") in new_pods:
                    mark(f"grafana[{r['name']}]={a['state']}")
                    if a["state"] in ("Alerting", "firing"):
                        alerting.add(r["name"])
    for i in (http("http://localhost:18095/incidents", aa) or []):
        if i in base_incidents or i in judged or inc is not None:
            continue
        rep = (http(f"http://localhost:18095/incidents/{i}/report", aa) or {}).get("report", "")
        if "Audit trail" not in rep:
            continue                                   # still being processed: look again next round
        judged.add(i)
        if "(firing," in rep.split("\n- **Affected")[0]:
            inc = i
            mark("agent_incident_created_by_grafana_alert", incident=inc)
        else:
            mark(f"ignored_resolved_notification[{i[-6:]}]")
    if inc and "agent_report_ready" not in ev:
        rep = (http(f"http://localhost:18095/incidents/{inc}/report", aa) or {}).get("report", "")
        if "Audit trail" in rep:
            mark("agent_report_ready")
            OUT.with_suffix(".report.md").write_text(rep)
    if "agent_report_ready" in ev and (set(EXPECTED) <= alerting and now() - ev["agent_report_ready"]["t"] > 20 or now() - ev["agent_report_ready"]["t"] > 150):
        break
    time.sleep(2)
json.dump({"scenario": args.scenario, "events": ev, "incident": inc, "alerting": sorted(alerting)}, OUT.open("w"), indent=1)
print(f"OBSERVER DONE incident={inc} alerting={sorted(alerting)} timeline={OUT}", flush=True)
sys.exit(0 if inc else 1)
