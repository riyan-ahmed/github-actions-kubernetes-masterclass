"""Run the GitOps self-healing trials end to end, with the safety gates enforced in code.

  aiops/.venv/bin/python aiops/scenarios/tools/gitops_trials.py --runs 3

Per trial: inject (Git commit) -> confirm a real OOMKilled -> wait for the agent's PR -> VALIDATE the PR -> merge
(`gh pr merge`, never `--admin`) -> wait for Argo CD + recovery -> measure -> reset (Git commit) -> verify baseline.

The merge in these trials is performed by this script after automated checks, under an explicit delegation from the
repository owner. It is NOT a person reading the diff, and the results say so.

The script never runs a Kubernetes write: it only reads (kubectl get / port-forward), calls `gh`, and runs
scripts/gitops-oom-demo.sh (Git commits only). Any violated gate raises Stop and the script exits non-zero with the
cluster left exactly as it is.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
CTX, NS, APP = "kind-skillpulse", "skillpulse-gitops", "oom-demo"
REPO, BRANCH = "riyan-ahmed/github-actions-kubernetes-masterclass", "gitops-demo"
FILE = "k8s/gitops/oom-demo/patches/resources.yaml"
DEMO = str(ROOT / "scripts" / "gitops-oom-demo.sh")
OUT = ROOT / "aiops" / "audit" / "gitops-runs"
COOLDOWN_S = 150          # > AIOPS_DEDUPE_WINDOW_S (120) so the next incident is not deduplicated


class Stop(Exception):
    pass


def now():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"[{now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run(*cmd, check=True, input=None):
    r = subprocess.run(cmd, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise Stop(f"command failed: {' '.join(cmd[:4])}... -> {r.stderr.strip()[:300]}")
    return r.stdout


def kjson(*args):
    return json.loads(run("kubectl", "--context", CTX, *args, "-o", "json"))


def gh_api(path, *extra):
    return json.loads(run("gh", "api", path, *extra))


_last = {"wall": time.time(), "mono": time.monotonic()}


def check_clock():
    """Abort if the machine slept: wall time advances during sleep, monotonic time does not. A run that spans a sleep
    has meaningless timings (and a frozen cluster), so it must not continue."""
    w, m = time.time(), time.monotonic()
    drift = (w - _last["wall"]) - (m - _last["mono"])
    _last.update(wall=w, mono=m)
    if drift > 20:
        raise Stop(f"the machine appears to have slept for ~{int(drift)}s; this run's timings would be invalid")


def wait(fn, timeout, interval, what):
    start = time.monotonic()
    check_clock()
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() - start > timeout:
            raise Stop(f"timeout after {timeout}s waiting for: {what}")
        time.sleep(interval)
        check_clock()


def secs(a, b):
    return round((b - a).total_seconds(), 1)


# ---- agent API through one persistent port-forward (token held in memory, never printed) ---------------------------
class Agent:
    def __init__(self):
        tok = run("kubectl", "--context", CTX, "-n", "aiops", "get", "secret", "aiops-webhook", "-o", "jsonpath={.data.token}")
        self.token = base64.b64decode(tok).decode().strip()
        self.pf = subprocess.Popen(["kubectl", "--context", CTX, "-n", "aiops", "port-forward", "svc/aiops-agent", "18086:8085"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait(lambda: self._ok(), 30, 1, "agent port-forward")

    def _ok(self):
        try:
            urllib.request.urlopen("http://127.0.0.1:18086/healthz", timeout=3).read()
            return True
        except OSError:
            return False

    def get(self, path):
        req = urllib.request.Request("http://127.0.0.1:18086" + path, headers={"Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def close(self):
        self.pf.terminate()


# ---- gates ---------------------------------------------------------------------------------------------------------
def deployment_state():
    d = kjson("-n", NS, "get", "deploy", APP)
    c = d["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in c.get("env", [])}
    # pods already being deleted by the rollout are not part of the live set (the old pod lingers a few seconds)
    pods = [p for p in kjson("-n", NS, "get", "pods", "-l", f"app={APP}")["items"] if not p["metadata"].get("deletionTimestamp")]
    return {"limit": c["resources"]["limits"]["memory"], "alloc": env.get("ALLOC_MB"), "pods": pods,
            "ready": d["status"].get("readyReplicas", 0), "revision": d["metadata"]["annotations"].get("deployment.kubernetes.io/revision")}


def argo():
    a = kjson("-n", "argocd", "get", "application", APP)
    st, op = a.get("status", {}), a.get("status", {}).get("operationState", {})
    return {"sync": st.get("sync", {}).get("status"), "revision": st.get("sync", {}).get("revision"),
            "health": st.get("health", {}).get("status"), "op_phase": op.get("phase"), "op_started": op.get("startedAt"),
            "op_finished": op.get("finishedAt"), "op_revision": op.get("syncResult", {}).get("revision")}


def baseline_ok():
    s = deployment_state()
    pods_ok = len(s["pods"]) == 1 and all(
        c["ready"] and c["restartCount"] == 0 for p in s["pods"] for c in p["status"].get("containerStatuses", []))
    a = argo()
    return s["limit"] == "32Mi" and s["alloc"] == "12" and pods_ok and a["sync"] == "Synced" and a["health"] == "Healthy"


def head_sha():
    return gh_api(f"repos/{REPO}/branches/{BRANCH}")["commit"]["sha"]


def oom_pod():
    for p in kjson("-n", NS, "get", "pods", "-l", f"app={APP}")["items"]:
        for c in p["status"].get("containerStatuses", []):
            for st in (c.get("lastState", {}).get("terminated"), c.get("state", {}).get("terminated")):
                if st and st.get("reason") == "OOMKilled":
                    return {"pod": p["metadata"]["name"], "exit_code": st.get("exitCode"), "finished_at": st.get("finishedAt"),
                            "restarts": c["restartCount"], "limit": p["spec"]["containers"][0]["resources"]["limits"]["memory"]}
    return None


def get_file(ref):
    r = gh_api(f"repos/{REPO}/contents/{FILE}?ref={ref}")
    return base64.b64decode(r["content"]).decode()


def validate_pr(pr_num):
    """Every gate from the delegation. Raises Stop on the first violation. Returns the evidence."""
    pr = gh_api(f"repos/{REPO}/pulls/{pr_num}")
    ev = {"number": pr_num, "url": pr["html_url"], "head_sha": pr["head"]["sha"], "head_ref": pr["head"]["ref"],
          "created_at": pr["created_at"], "checks": {}}
    if pr["base"]["ref"] != BRANCH:
        raise Stop(f"PR targets {pr['base']['ref']}, not {BRANCH}")
    if pr["head"]["repo"]["full_name"] != REPO or not pr["head"]["ref"].startswith("aiops/inc-"):
        raise Stop(f"unexpected PR head {pr['head']['label']}")
    files = gh_api(f"repos/{REPO}/pulls/{pr_num}/files")
    if [f["filename"] for f in files] != [FILE] or files[0]["status"] != "modified":
        raise Stop(f"PR changes unexpected files: {[(f['filename'], f['status']) for f in files]}")
    base_txt, head_txt = get_file(BRANCH), get_file(pr["head"]["sha"])
    bd, hd = yaml.safe_load(base_txt), yaml.safe_load(head_txt)
    bm = bd["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]
    hm = hd["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]
    if (bm, hm) != ("32Mi", "64Mi"):
        raise Stop(f"memory limit change is {bm} -> {hm}, expected 32Mi -> 64Mi")
    hd["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = bm
    if hd != bd:
        raise Stop("PR changes something other than limits.memory")
    changed = [l for l in difflib.unified_diff(base_txt.splitlines(), head_txt.splitlines(), lineterm="", n=0)
               if l[:1] in "+-" and l[:3] not in ("+++", "---")]
    if len(changed) != 2 or "memory" not in changed[0] or "memory" not in changed[1]:
        raise Stop(f"text diff is not a single memory line: {changed}")
    ev["diff"] = changed
    if pr["commits"] != 1:
        raise Stop(f"PR has {pr['commits']} commits, expected 1")
    # checks: combined status + check runs on the head commit
    status = gh_api(f"repos/{REPO}/commits/{pr['head']['sha']}/status")
    runs = gh_api(f"repos/{REPO}/commits/{pr['head']['sha']}/check-runs")
    ev["checks"] = {"combined_status": status["state"], "status_contexts": len(status["statuses"]), "check_runs": runs["total_count"]}
    bad = [r["name"] for r in runs["check_runs"] if r["conclusion"] not in ("success", "neutral", "skipped")]
    if bad or status["state"] in ("failure", "error"):
        raise Stop(f"checks not passing: {bad or status['state']}")
    ev["checks"]["note"] = ("all passing" if status["statuses"] or runs["total_count"]
                            else "no status checks or workflows are configured for this branch (none applicable)")
    for _ in range(10):      # mergeability is computed lazily by GitHub
        pr = gh_api(f"repos/{REPO}/pulls/{pr_num}")
        if pr["mergeable"] is not None:
            break
        time.sleep(2)
    if pr["mergeable"] is not True or pr["mergeable_state"] not in ("clean", "unstable", "has_hooks"):
        raise Stop(f"PR not cleanly mergeable: {pr['mergeable']} / {pr['mergeable_state']}")
    ev["mergeable_state"] = pr["mergeable_state"]
    return ev


def agent_gates(agent, iid):
    """Audit-level gates: the policy path, the diagnosis, and that no Kubernetes write was attempted."""
    ev = agent.get(f"/incidents/{iid}/events")
    by = {}
    for e in ev:
        by.setdefault(e["event"], []).append(e)
    pol = by.get("policy_decision", [{}])[0].get("data", {})
    diag = by.get("diagnosis", [{}])[0].get("data", {}).get("classification", {})
    if pol.get("decision") != "pr_allowed" or pol.get("action") != "propose_config_pr":
        raise Stop(f"unexpected policy decision: {pol}")
    if diag.get("incident_type") != "oom_killed":
        raise Stop(f"unexpected diagnosis: {diag.get('incident_type')}")
    for e in by.get("action_executed", []):
        if e["data"].get("action") != "propose_config_pr":
            raise Stop(f"agent executed a non-PR action: {e['data'].get('action')}")
    if any(e["event"] in ("action_blocked", "action_failed") for e in ev):
        raise Stop("agent action blocked/failed")
    return {"policy": pol, "classification": {k: diag.get(k) for k in ("incident_type", "root_cause", "confidence", "evidence_sources")},
            "runbook": by.get("diagnosis", [{}])[0].get("data", {}).get("runbook")}


# ---- one trial -----------------------------------------------------------------------------------------------------
def trial(n, agent):
    rec = {"run": n, "started_at": now().isoformat(), "reintroduction": None}
    log(f"=== RUN {n}: preconditions")
    if not baseline_ok():
        raise Stop("workload is not at the healthy baseline (limit 32Mi, ALLOC_MB 12, 1 Ready pod, 0 restarts, Synced/Healthy)")
    run(DEMO, "preflight")
    open_before = {p["number"] for p in gh_api(f"repos/{REPO}/pulls?base={BRANCH}&state=open")}
    if open_before:
        raise Stop(f"open PRs already exist against {BRANCH}: {sorted(open_before)}")

    log("injecting failure (Git commit ALLOC_MB 12 -> 40 on gitops-demo)")
    run(DEMO, "inject")
    inj = json.loads((OUT / "last-inject.json").read_text())
    rec["injection"] = {**inj, "how": "scripts/gitops-oom-demo.sh inject: Git commit raising ALLOC_MB 12 -> 40 on gitops-demo; synced by Argo CD"}
    log(f"injected at {inj['injected_at']} commit {inj['commit'][:10]}")

    oom = wait(oom_pod, 600, 3, "a genuine OOMKilled container")
    if oom["exit_code"] != 137:
        raise Stop(f"OOM exit code {oom['exit_code']}, expected 137")
    rec["oom"] = oom
    log(f"OOMKilled confirmed: {oom['pod']} exit {oom['exit_code']} at {oom['finished_at']} (limit {oom['limit']})")

    def new_pr():
        prs = gh_api(f"repos/{REPO}/pulls?base={BRANCH}&state=open")
        return prs[0]["number"] if prs else None
    pr_num = wait(new_pr, 600, 3, "the agent's pull request")
    log(f"agent opened PR #{pr_num}")
    pr_ev = validate_pr(pr_num)
    iid = pr_ev["head_ref"].removeprefix("aiops/")
    rec["pr"] = pr_ev
    rec["agent_gates"] = agent_gates(agent, iid)
    log(f"PR #{pr_num} validated ({pr_ev['head_sha'][:10]}, diff {pr_ev['diff']}, checks: {pr_ev['checks']['note']})")

    log("merging (gh pr merge --merge, pinned to the validated head commit; no --admin)")
    run("gh", "pr", "merge", str(pr_num), "--repo", REPO, "--merge", "--match-head-commit", pr_ev["head_sha"])
    merged = gh_api(f"repos/{REPO}/pulls/{pr_num}")
    if not merged["merged"]:
        raise Stop("PR is not merged after gh pr merge")
    rec["merge"] = {"merged_at": merged["merged_at"], "merge_commit_sha": merged["merge_commit_sha"], "merged_by": merged["merged_by"]["login"],
                    "performed_by": "gitops_trials.py after automated validation (delegated by the repository owner; not a manual review)"}
    log(f"merged at {merged['merged_at']} sha {merged['merge_commit_sha'][:10]}")

    def argo_at_merge():
        a = argo()
        return a if (a["op_revision"] == merged["merge_commit_sha"] and a["op_phase"] == "Succeeded" and a["sync"] == "Synced") else None
    a = wait(argo_at_merge, 900, 3, "Argo CD to sync the merge commit")
    rec["argocd"] = a
    log(f"Argo CD synced {a['op_revision'][:10]} (op started {a['op_started']}, finished {a['op_finished']})")

    def tracker_done():
        events = agent.get(f"/incidents/{iid}/events")
        names = [e["event"] for e in events]
        for bad in ("tracker_failed", "argocd_sync_timeout", "recovery_not_verified", "pr_wait_timeout", "pr_closed_unmerged"):
            if bad in names:
                raise Stop(f"tracker reported {bad}")
        return events if "gitops_recovery_complete" in names else None
    events = wait(tracker_done, 600, 3, "the agent's tracker to confirm recovery")

    s = deployment_state()
    a2 = argo()
    pods_ok = len(s["pods"]) == 1 and all(c["ready"] and c["restartCount"] == 0 for p in s["pods"] for c in p["status"].get("containerStatuses", []))
    if not (s["limit"] == "64Mi" and pods_ok and a2["sync"] == "Synced" and a2["health"] == "Healthy" and a2["revision"] == merged["merge_commit_sha"]):
        raise Stop(f"independent recovery check failed: {s['limit']} pods_ok={pods_ok} argo={a2}")
    rec["recovered"] = {"limit": s["limit"], "argocd": a2, "pod": s["pods"][0]["metadata"]["name"]}
    log(f"RECOVERED: limit {s['limit']}, Argo CD {a2['sync']}/{a2['health']} at {a2['revision'][:10]}")

    out = run(sys.executable, str(ROOT / "aiops/scenarios/tools/gitops_measure.py"), iid, "--context", CTX, "--inject-file", str(OUT / "last-inject.json"),
               "--first-oom", rec["oom"]["finished_at"])
    rec["measurements"] = json.loads(out)
    rec["incident_id"] = iid
    rec["audit_events"] = [{"ts": e["ts"], "event": e["event"], "actor": e["actor"]} for e in events]
    rec["audit_chain"] = run("kubectl", "--context", CTX, "-n", "aiops", "exec", "deploy/aiops-agent", "--", "python", "-m", "aiops.agent", "audit-verify").strip()
    if "ok" not in rec["audit_chain"]:
        raise Stop(f"audit chain not valid: {rec['audit_chain']}")
    log(f"measurements: {json.dumps(rec['measurements']['seconds'])}")

    return rec, iid


def reset(rec):
    log("resetting to baseline (Git commit: ALLOC_MB 12, limit 32Mi)")
    before = head_sha()
    out = run(DEMO, "reset")
    sha = head_sha()
    if sha == before:
        raise Stop("reset made no commit")
    rec["reset"] = {"commit": sha, "how": "scripts/gitops-oom-demo.sh reset: Git commit restoring ALLOC_MB=12 and limits.memory=32Mi on gitops-demo; synced by Argo CD",
                    "at": now().isoformat()}
    wait(baseline_ok, 900, 3, "Argo CD to restore the healthy baseline")
    if argo()["revision"] != sha:
        raise Stop("Argo CD baseline is not at the reset commit")
    log(f"baseline restored at {sha[:10]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--start", type=int, default=1)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    agent = Agent()
    results = []
    try:
        for n in range(a.start, a.start + a.runs):
            rec, iid = trial(n, agent)
            reset(rec)
            rec["finished_at"] = now().isoformat()
            (OUT / f"run-{n}.json").write_text(json.dumps(rec, indent=1))
            results.append(rec)
            log(f"RUN {n} COMPLETE -> {OUT / f'run-{n}.json'}")
            if n < a.start + a.runs - 1:
                log(f"cooldown {COOLDOWN_S}s (agent dedupe window)")
                check_clock()
                time.sleep(COOLDOWN_S)
                check_clock()
    except Stop as e:
        log(f"STOP: {e}")
        log("The cluster is left exactly as it is. Nothing was patched, rolled back or synced by this script.")
        return 1
    finally:
        agent.close()
    log(f"ALL {len(results)} RUNS COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
