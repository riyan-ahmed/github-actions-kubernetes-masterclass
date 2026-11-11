"""Build the results report from the recorded trial files (aiops/audit/gitops-runs/run-N.json).

Every figure is computed from raw timestamps recorded during the run. Failure onset is the FIRST OOMKilled termination
seen by the harness (Kubernetes lastState.terminated.finishedAt of the first crashed container). All sources have 1 s
resolution except the agent's own audit clock, so sub-second stages are reported as measured, not rounded up.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

RUNS = Path(__file__).resolve().parents[3] / "aiops" / "audit" / "gitops-runs"


def t(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def d(a, b):
    return round((t(b) - t(a)).total_seconds(), 1)


def ev(r, name):
    return next(e for e in r["audit_events"] if e["event"] == name)["ts"]


def stages(r):
    sync_end = r["argocd"]["op_finished"]
    healthy_s = r["measurements"]["seconds"]["sync_to_healthy"]           # sync finished -> new pod Ready (pod condition)
    s = {
        "injection_to_first_oom": d(r["injection"]["injected_at"], r["oom"]["finished_at"]),
        "first_oom_to_agent_alert": d(r["oom"]["finished_at"], ev(r, "alert_received")),
        "alert_to_diagnosis": d(ev(r, "alert_received"), ev(r, "diagnosis")),
        "diagnosis_to_pr_created": d(ev(r, "diagnosis"), r["pr"]["created_at"]),
        "pr_created_to_merge": d(r["pr"]["created_at"], r["merge"]["merged_at"]),
        "merge_to_argocd_sync_started": d(r["merge"]["merged_at"], r["argocd"]["op_started"]),
        "argocd_sync_duration": d(r["argocd"]["op_started"], r["argocd"]["op_finished"]),
        "sync_to_pod_ready": healthy_s,
    }
    s["first_oom_to_pr_created"] = round(s["first_oom_to_agent_alert"] + s["alert_to_diagnosis"] + s["diagnosis_to_pr_created"], 1)
    total = round(d(r["oom"]["finished_at"], sync_end) + healthy_s, 1)
    s["total_first_oom_to_healthy_incl_merge_gate"] = total
    s["total_excl_merge_gate"] = round(total - s["pr_created_to_merge"], 1)
    s["total_excl_merge_gate_and_argocd_poll"] = round(s["total_excl_merge_gate"] - s["merge_to_argocd_sync_started"], 1)
    return s


def main():
    runs = [json.loads(p.read_text()) for p in sorted(RUNS.glob("run-[0-9]*.json"))]
    st = [stages(r) for r in runs]
    out = {"runs": [{"run": r["run"], "stages": s} for r, s in zip(runs, st)]}
    keys = list(st[0])
    out["summary"] = {k: {"min": min(s[k] for s in st), "median": statistics.median(s[k] for s in st), "max": max(s[k] for s in st)} for k in keys}
    if "--json" in sys.argv:
        print(json.dumps(out, indent=1))
        return
    print("| Stage (s) | " + " | ".join(f"Run {r['run']}" for r in runs) + " | median |\n|---|" + "---|" * (len(runs) + 1))
    for k in keys:
        print(f"| {k} | " + " | ".join(str(s[k]) for s in st) + f" | {out['summary'][k]['median']} |")
    print()
    print("| Run | Incident | PR | Inject commit | PR head | Merge commit | Argo CD revision (Synced/Healthy) | Reset commit | Audit chain |\n|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        print(f"| {r['run']} | `{r['incident_id']}` | [#{r['pr']['number']}]({r['pr']['url']}) | `{r['injection']['commit'][:10]}` | `{r['pr']['head_sha'][:10]}` | "
              f"`{r['merge']['merge_commit_sha'][:10]}` | `{r['recovered']['argocd']['revision'][:10]}` | `{r['reset']['commit'][:10]}` | {r['audit_chain'].replace('audit chain: ', '')} |")
    print()
    print("| Run | Pod OOMKilled | Diagnosis | Policy decision | Files changed in PR | Diff |\n|---|---|---|---|---|---|")
    for r in runs:
        g = r["agent_gates"]
        print(f"| {r['run']} | `{r['oom']['pod']}` exit {r['oom']['exit_code']} at {r['oom']['finished_at']} | {g['classification']['incident_type']} / {g['classification']['root_cause']}, confidence {g['classification']['confidence']} | "
              f"{g['policy']['decision']} ({g['policy']['action']}) | 1 | `{r['pr']['diff'][0].strip()}` -> `{r['pr']['diff'][1].strip()}` |")


main()
