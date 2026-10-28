"""Recovery verification: judge a workload from a fresh Kubernetes snapshot."""
from __future__ import annotations

import time
from typing import Callable


def evaluate_recovery(snapshot: dict, baseline_restarts: dict[str, int] | None = None) -> dict:
    """Pure function over the dict returned by KubeClient.collect()."""
    dep = snapshot["deployment"]
    desired = dep["replicas_desired"] or 0
    checks = []

    def check(name: str, ok: bool, detail: str):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    check("rollout_observed", dep["observed_generation"] is not None and dep["observed_generation"] >= dep["generation"],
          f"observedGeneration={dep['observed_generation']} generation={dep['generation']}")
    check("replicas_available", dep["replicas_available"] >= desired and desired > 0,
          f"{dep['replicas_available']}/{desired} available")
    check("replicas_updated", dep["replicas_updated"] >= desired, f"{dep['replicas_updated']}/{desired} updated")
    bad = [f"{p['name']}/{c['name']}:{(c['state'] or {}).get('reason')}" for p in snapshot["pods"] for c in p["containers"]
           if (c["state"] or {}).get("reason") in ("CrashLoopBackOff", "OOMKilled", "Error", "ImagePullBackOff", "ErrImagePull")]
    check("no_failing_containers", not bad, ", ".join(bad) or "none")
    not_ready = [p["name"] for p in snapshot["pods"] if not p["ready"]]
    check("all_pods_ready", snapshot["pods"] and not not_ready, f"not ready: {not_ready or 'none'}")
    if baseline_restarts is not None:
        grew = {f"{p['name']}/{c['name']}": c["restart_count"] for p in snapshot["pods"] for c in p["containers"]
                if c["restart_count"] > baseline_restarts.get(f"{p['name']}/{c['name']}", c["restart_count"])}
        check("no_new_restarts", not grew, str(grew or "none"))
    return {"recovered": all(c["ok"] for c in checks), "checks": checks}


def wait_for_recovery(fetch: Callable[[], dict], timeout_s: int, interval_s: int,
                      clock: Callable[[], float] = time.monotonic,
                      sleep: Callable[[float], None] = time.sleep) -> dict:
    """Poll until healthy or timeout. A single healthy snapshot is not enough: require two
    consecutive healthy polls so a pod that is briefly Ready before failing again is not
    reported as recovered."""
    start, streak, result = clock(), 0, {"recovered": False, "checks": []}
    while True:
        result = evaluate_recovery(fetch())
        streak = streak + 1 if result["recovered"] else 0
        if streak >= 2:
            return {**result, "status": "recovered", "waited_s": round(clock() - start, 1)}
        if clock() - start >= timeout_s:
            return {**result, "recovered": False, "status": "not_recovered", "waited_s": round(clock() - start, 1)}
        sleep(interval_s)
