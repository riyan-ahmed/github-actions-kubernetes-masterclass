"""Executors for the allowlisted actions. Only reachable through execute_action().

No shell, no kubectl subprocess: every write goes through the typed Kubernetes API.
The target always comes from the incident record, never from action parameters.
"""
from __future__ import annotations

from .gitops import propose_config_change
from .policy import Policy, PolicyError, validate_action_request
from .util import iso, utcnow


def execute_action(*, action_id: str, params: dict, target: dict, kube, prom, policy: Policy, settings,
                   incident_id: str, rationale: str, dry_run: bool) -> dict:
    params = validate_action_request(action_id, params, policy.memory_cap)  # re-validate at the last moment
    ns, name = target["namespace"], target["name"]

    if action_id == "rollout_undo":
        prev = kube.previous_healthy_revision(ns, name)           # exact Deployment, exact older ReplicaSet
        ready = prom.was_ready(ns, prev["name"]) if prom is not None else {"was_ready": False, "error": "prometheus unavailable"}
        if not ready.get("was_ready"):
            raise PolicyError(f"rollback target {prev['name']} is not proven healthy; refusing to roll back")
        result = kube.rollout_undo(ns, name, dry_run)
        return {"action": action_id, "dry_run": dry_run, "namespace": ns, "workload": name,
                "target_replicaset_ready_history": ready, **result}
    if action_id == "restart_deployment":
        result = kube.restart(ns, name, iso(utcnow()), dry_run)
        return {"action": action_id, "dry_run": dry_run, "namespace": ns, "workload": name, **result}
    if action_id == "propose_config_pr":
        result = propose_config_change(settings=settings, policy=policy, incident_id=incident_id, namespace=ns,
                                       workload=name, params=params, rationale=rationale, dry_run=dry_run)
        return {"action": action_id, "dry_run": dry_run, "namespace": ns, "workload": name, **result}
    return {"action": "escalate_human", "dry_run": dry_run, "namespace": ns, "workload": name, "executed": False}
