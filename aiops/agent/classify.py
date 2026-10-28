"""Deterministic incident classification and action selection from collected evidence.

Alert names are only hints. The classification is derived from Kubernetes state,
Prometheus series and log lines, and each conclusion lists the signals behind it.
"""
from __future__ import annotations

import re

from .models import INCIDENT_TYPES  # noqa: F401  (re-exported for callers)
from .util import format_mi, parse_quantity


def _containers(k8s: dict):
    for p in k8s.get("pods", []):
        for c in p["containers"]:
            yield p, c


def _log_text(evidence: dict) -> str:
    lines = [l["line"] for l in evidence.get("logs", {}).get("lines", [])]
    for ls in evidence.get("k8s", {}).get("previous_logs", {}).values():
        lines += ls
    return "\n".join(lines)


def _event_text(k8s: dict) -> str:
    return "\n".join(f"{e['reason']}: {e['message']}" for e in k8s.get("events", []))


def _prom_series(evidence: dict, name: str) -> list[dict]:
    return evidence.get("metrics", {}).get(name, {}).get("result", []) or []


def classify(evidence: dict) -> dict:
    k8s = evidence.get("k8s", {})
    signals: list[dict] = []
    add = lambda src, text: signals.append({"source": src, "signal": text})  # noqa: E731
    logs, events = _log_text(evidence), _event_text(k8s)

    oom = [(p, c) for p, c in _containers(k8s)
           if (c.get("last_state") or {}).get("reason") == "OOMKilled" or (c.get("state") or {}).get("reason") == "OOMKilled"]
    crash = [(p, c) for p, c in _containers(k8s) if (c.get("state") or {}).get("reason") == "CrashLoopBackOff"]
    notready = [(p, c) for p, c in _containers(k8s)
                if (c.get("state") or {}).get("kind") == "running" and not c["ready"]]
    prom_oom = [s for s in _prom_series(evidence, "last_terminated_reason") if s["metric"].get("reason") == "OOMKilled"]
    prom_crash = [s for s in _prom_series(evidence, "waiting_reason") if s["metric"].get("reason") == "CrashLoopBackOff"]

    # ---- OOM (checked first: OOM loops also surface as CrashLoopBackOff) ----
    if oom or prom_oom:
        for p, c in oom:
            ls = c.get("last_state") or {}
            add("k8s", f"container {c['name']} in {p['name']} terminated with reason OOMKilled "
                       f"(exit code {ls.get('exit_code')}), restarts={c['restart_count']}")
        if prom_oom:
            add("prometheus", f"kube_pod_container_status_last_terminated_reason{{reason=OOMKilled}} == 1 for {len(prom_oom)} series")
        limit_changes = [d for d in k8s.get("revision_diff", []) if ".memory_limit" in d]
        if limit_changes:
            add("k8s", f"memory limit changed between revisions: {limit_changes[0]}")
            cause, summary = "memory_limit_reduced", "Memory limit was lowered in the latest rollout and the container exceeds it."
        else:
            cause, summary = "memory_limit_exceeded", "The container exceeds its memory limit and is killed by the kernel OOM killer."
        if "OOMKilled" in events:
            add("k8s-events", "event mentions OOMKilled")
        return _result("oom_killed", cause, summary, signals, evidence)

    # ---- CrashLoopBackOff ----
    if crash or prom_crash:
        for p, c in crash:
            ls = c.get("last_state") or {}
            add("k8s", f"container {c['name']} in {p['name']} is CrashLoopBackOff; last exit code "
                       f"{ls.get('exit_code')} ({ls.get('reason')}), restarts={c['restart_count']}")
        if prom_crash:
            add("prometheus", f"kube_pod_container_status_waiting_reason{{reason=CrashLoopBackOff}} == 1 for {len(prom_crash)} series")
        env_changes = [d for d in k8s.get("revision_diff", []) if ".env." in d or ".image" in d]
        if re.search(r"unknown port|invalid port|listen tcp.*(?:address|bind)", logs, re.I):
            add("logs", "application cannot bind/listen on its configured port")
            cause, summary = "invalid_configuration", "The process exits at startup because its port configuration is invalid."
        elif re.search(r"could not connect to database|waiting for database", logs, re.I):
            add("logs", "application reports it cannot reach the database")
            cause, summary = "database_unreachable", "The process exits because it cannot connect to its database."
        elif re.search(r"panic:|fatal error:|segmentation fault", logs, re.I):
            add("logs", "application panic / fatal error in logs")
            cause, summary = "application_panic", "The process crashes with a panic or fatal error."
        else:
            cause, summary = "crash_unknown", "The container repeatedly exits; logs do not show a recognised cause."
        if env_changes:
            add("k8s", f"latest rollout changed: {env_changes[0]}")
        return _result("crash_loop", cause, summary, signals, evidence)

    # ---- Readiness failure ----
    pods_not_ready = [p for p in k8s.get("pods", []) if not p["ready"]]
    dep = k8s.get("deployment", {})
    if notready or (pods_not_ready and dep.get("replicas_available", 0) < dep.get("replicas_desired", 0)):
        for p, c in notready:
            add("k8s", f"container {c['name']} in {p['name']} is Running but not Ready")
        if dep:
            add("k8s", f"deployment has {dep.get('replicas_available')}/{dep.get('replicas_desired')} replicas available")
        m = re.search(r"Readiness probe failed: (.*)", events)
        if m:
            add("k8s-events", f"Readiness probe failed: {m.group(1)[:120]}")
        if re.search(r"statuscode: 404|404", events + logs) and re.search(r"readiness probe failed", events, re.I):
            cause, summary = "probe_misconfigured", "The readiness probe receives HTTP 404; its path does not exist on the application."
        elif re.search(r"connection refused", events, re.I):
            cause, summary = "app_not_listening", "The readiness probe is refused; the application is not listening on the probed port."
        elif re.search(r"statuscode: 503|unhealthy", events + logs, re.I):
            cause, summary = "dependency_unavailable", "The application reports unhealthy, most likely because a dependency (database) is down."
        else:
            cause, summary = "readiness_unknown", "Pods are Running but not Ready; the probe failure reason is not recognised."
        probe_change = [d for d in k8s.get("revision_diff", []) if "readiness_path" in d]
        if probe_change:
            add("k8s", f"latest rollout changed readiness probe: {probe_change[0]}")
        return _result("readiness_failure", cause, summary, signals, evidence)

    return _result("unclassified", "no_failure_signal",
                   "No OOM, crash-loop or readiness signal was found in the collected evidence.", signals, evidence)


def _result(itype: str, cause: str, summary: str, signals: list[dict], evidence: dict) -> dict:
    sources = {s["source"].split("-")[0] for s in signals}
    for k in ("logs",):
        if evidence.get(k, {}).get("lines") or evidence.get("k8s", {}).get("previous_logs"):
            sources.add("logs")
    # Heuristic score from the number of independent evidence sources; NOT a calibrated probability.
    confidence = 0.2 if itype == "unclassified" else min(0.95, 0.45 + 0.15 * len(sources))
    return {"incident_type": itype, "root_cause": cause, "summary": summary,
            "signals": signals, "confidence": round(confidence, 2),
            "evidence_sources": sorted(sources)}


def propose_action(classification: dict, evidence: dict, memory_cap: str = "512Mi") -> dict:
    """Pick one action from the closed registry. Pure function of the evidence."""
    itype, cause = classification["incident_type"], classification["root_cause"]
    k8s = evidence.get("k8s", {})
    diff = k8s.get("revision_diff", [])
    prev_ready = evidence.get("metrics", {}).get("previous_revision", {}).get("was_ready")
    recent_rollout = bool(diff)

    def undo(reason: str) -> dict:
        if not recent_rollout:
            return escalate("no configuration difference between revisions to roll back")
        if prev_ready is not True:
            return escalate("previous revision is not proven healthy (no Ready pods observed in Prometheus history); "
                            "rollback would be a guess")
        return {"action_id": "rollout_undo", "params": {}, "rationale": reason}

    def escalate(why: str) -> dict:
        return {"action_id": "escalate_human", "params": {"reason": why[:300]}, "rationale": why}

    if itype == "oom_killed":
        if cause == "memory_limit_reduced":
            return undo("Latest rollout lowered the memory limit; the previous revision ran with a higher limit.")
        dep_cont = next(iter(next(iter(k8s.get("revisions", [{}])), {}).get("template", {}).get("containers", {}).items()), None)
        if dep_cont and dep_cont[1].get("memory_limit"):
            new = min(parse_quantity(dep_cont[1]["memory_limit"]) * 2, parse_quantity(memory_cap))
            if new > parse_quantity(dep_cont[1]["memory_limit"]):
                return {"action_id": "propose_config_pr",
                        "params": {"container": dep_cont[0], "memory_limit": format_mi(new)},
                        "rationale": f"Raise memory limit {dep_cont[1]['memory_limit']} -> {format_mi(new)} via a reviewed Git change."}
        return escalate("OOM without a safe automatic memory increase (limit unknown or at cap)")
    if itype == "crash_loop":
        if cause == "database_unreachable":
            return escalate("database unreachable: restarting or rolling back the backend will not help; inspect MySQL")
        return undo("The crash loop started with the latest rollout; revert to the previous revision.")
    if itype == "readiness_failure":
        if cause == "dependency_unavailable":
            return escalate("dependency unavailable: do not restart the backend; inspect MySQL readiness")
        if cause in ("probe_misconfigured", "app_not_listening"):
            return undo("Readiness failure follows a rollout that changed the pod spec; revert it.")
        return escalate("readiness failure with unrecognised cause")
    return escalate("incident could not be classified from the evidence")
