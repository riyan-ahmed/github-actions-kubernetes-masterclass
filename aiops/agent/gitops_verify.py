"""Close the GitOps loop: PR opened -> human merges -> Argo CD syncs -> workload healthy -> audit timeline.

The tracker only OBSERVES (GitHub PR status, the Argo CD Application, a Kubernetes snapshot). It performs no
write of any kind. Every step is appended to the hash-chained audit log, so the incident timeline can be
rebuilt and verified later with build_timeline() / `python -m aiops.agent timeline <incident>`.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .util import iso, parse_ts, utcnow
from .verify import evaluate_recovery

ACTOR = "gitops-tracker"


class PRTracker:
    def __init__(self, *, settings, audit, kube, argo, github, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.s, self.audit, self.kube, self.argo, self.github = settings, audit, kube, argo, github
        self.sleep, self.clock = sleep, clock

    # -- background entry point -------------------------------------------------------------------
    def start(self, incident_id: str, target: dict, detail: dict) -> threading.Thread:
        t = threading.Thread(target=self._safe_track, args=(incident_id, target, detail), daemon=True,
                             name=f"tracker-{incident_id}")
        t.start()
        return t

    def _safe_track(self, incident_id, target, detail):
        try:
            self.track(incident_id, target, detail)
        except Exception as e:  # never let the tracker die silently
            self.audit.record(incident_id, "tracker_failed", ACTOR, {"error": f"{type(e).__name__}: {e}"[:300]})

    # -- the loop ---------------------------------------------------------------------------------
    def _wait(self, fn, timeout_s):
        start = self.clock()
        while True:
            v = fn()
            if v is not None:
                return v
            if self.clock() - start >= timeout_s:
                return None
            self.sleep(self.s.tracker_poll_s)

    def track(self, incident_id: str, target: dict, detail: dict) -> dict:
        ns, name, number = target["namespace"], target["name"], detail["number"]
        app = detail.get("argocd_app")
        self.audit.record(incident_id, "recovery_tracking_started", ACTOR,
                          {"pr": detail.get("url"), "pr_number": number, "argocd_app": app})

        # 1. a human merges (or closes) the PR
        def pr_done():
            pr = self.github.get_pr(number)
            return pr if pr["state"] == "closed" or pr["merged"] else None
        pr = self._wait(pr_done, self.s.pr_wait_timeout_s)
        if pr is None:
            self.audit.record(incident_id, "pr_wait_timeout", ACTOR, {"waited_s": self.s.pr_wait_timeout_s})
            return {"status": "pr_not_merged"}
        if not pr["merged"]:
            self.audit.record(incident_id, "pr_closed_unmerged", ACTOR, {"closed_at": pr["closed_at"]})
            return {"status": "pr_rejected"}
        self.audit.record(incident_id, "pr_merged", ACTOR,
                          {"merged_at": pr["merged_at"], "merge_commit_sha": pr["merge_commit_sha"],
                           "merged_by": pr["merged_by"], "pr_number": number})
        sha = pr["merge_commit_sha"]

        # 2. Argo CD picks up the merge and syncs it
        if app:
            def synced():
                a = self.argo.get(app)
                at_sha = sha in (a["sync_revision"], a["operation_revision"])
                done = a["operation_phase"] == "Succeeded" and a["sync_status"] == "Synced"
                return a if (at_sha and done) else None
            a = self._wait(synced, self.s.argocd_wait_timeout_s)
            if a is None:
                self.audit.record(incident_id, "argocd_sync_timeout", ACTOR,
                                  {"expected_revision": sha, "waited_s": self.s.argocd_wait_timeout_s})
                return {"status": "argocd_not_synced"}
            self.audit.record(incident_id, "argocd_synced", ACTOR, {**a, "expected_revision": sha, "argocd_app": app})

        # 3. the corrected configuration is live and the workload is healthy
        def healthy():
            snap = self.kube.collect(ns, name, log_tail=0)
            res = evaluate_recovery(snap)
            cur = next((r for r in snap.get("revisions", []) if r["current"]), None)
            live_limit = ((cur or {}).get("template", {}).get("containers", {}).get(detail.get("container"), {}) or {}).get("memory_limit")
            res["checks"].append({"check": "proposed_limit_applied", "ok": live_limit == detail.get("memory_limit"),
                                  "detail": f"live={live_limit} proposed={detail.get('memory_limit')}"})
            res["recovered"] = all(c["ok"] for c in res["checks"])
            return res if res["recovered"] else None
        res = self._wait(healthy, self.s.verify_timeout_s)
        if res is None:
            self.audit.record(incident_id, "recovery_not_verified", ACTOR, {"waited_s": self.s.verify_timeout_s})
            return {"status": "not_recovered"}
        self.audit.record(incident_id, "workload_recovered", ACTOR, {**res, "observed_at": iso(utcnow())})
        timeline = build_timeline(self.audit.events(incident_id))
        self.audit.record(incident_id, "gitops_recovery_complete", ACTOR, {"timeline": timeline})
        return {"status": "recovered", "timeline": timeline}


# ------------------------------------------------------------------------------------------------
def _first(events: list[dict], name: str) -> dict | None:
    return next((e for e in events if e["event"] == name), None)


def _secs(a: str | None, b: str | None) -> float | None:
    ta, tb = parse_ts(a), parse_ts(b)
    return round((tb - ta).total_seconds(), 1) if ta and tb else None


def build_timeline(events: list[dict]) -> dict:
    """Stage durations in seconds, derived only from audit events (agent clock) and the GitHub/Argo timestamps
    recorded in them. A stage is None when its event has not happened (yet)."""
    alert = _first(events, "alert_received")
    diag = _first(events, "diagnosis")
    pr = next((e for e in events if e["event"] == "action_executed" and e["data"].get("mode") == "pull_request"), None)
    merged = _first(events, "pr_merged")
    synced = _first(events, "argocd_synced")
    rec = _first(events, "workload_recovered")
    t = lambda e: e["ts"] if e else None  # noqa: E731
    starts = alert["data"]["alert"].get("starts_at") if alert else None
    pr_created = (pr["data"].get("created_at") if pr else None) or t(pr)
    sync_started = synced["data"].get("operation_started_at") if synced else None
    sync_done = synced["data"].get("operation_finished_at") if synced else None
    stages = {
        "alert_firing_to_agent_s": _secs(starts, t(alert)),
        "detection_to_diagnosis_s": _secs(t(alert), t(diag)),
        "diagnosis_to_pr_created_s": _secs(t(diag), pr_created),
        "pr_open_to_human_merge_s": _secs(pr_created, merged["data"]["merged_at"] if merged else None),
        "merge_to_argocd_sync_started_s": _secs(merged["data"]["merged_at"] if merged else None, sync_started),
        "argocd_sync_duration_s": _secs(sync_started, sync_done),
        "argocd_sync_to_healthy_s": _secs(sync_done, rec["data"]["observed_at"] if rec else None),
        "total_agent_received_to_healthy_s": _secs(t(alert), rec["data"]["observed_at"] if rec else None),
    }
    return {"stages": stages, "pr_url": pr["data"].get("url") if pr else None,
            "merged_by": merged["data"].get("merged_by") if merged else None,
            "merge_commit_sha": merged["data"].get("merge_commit_sha") if merged else None}


def rebuild_tracking_inputs(events: list[dict]) -> tuple[dict, dict]:
    """Recover (target, pr_detail) for a PR-opening incident from its audit events (used by the `track` command)."""
    pr = next((e for e in events if e["event"] == "action_executed" and e["data"].get("mode") == "pull_request"), None)
    if pr is None:
        raise ValueError("incident has no pull-request action")
    d = pr["data"]
    return ({"namespace": d["namespace"], "name": d["workload"]},
            {k: d.get(k) for k in ("url", "number", "created_at", "branch", "base", "memory_limit", "container", "argocd_app")})
