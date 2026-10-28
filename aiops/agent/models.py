"""Shared constants and the LangGraph state definition."""
from __future__ import annotations

from typing import Any, TypedDict

INCIDENT_TYPES = ("oom_killed", "crash_loop", "readiness_failure", "unclassified")

# Closed set of actions. Nothing outside this tuple can ever be executed.
ACTION_IDS = ("rollout_undo", "restart_deployment", "propose_config_pr", "escalate_human")

DECISIONS = ("advisory_only", "requires_approval", "auto_allowed", "denied")
MODES = ("diagnose", "approval", "auto")


class IncidentState(TypedDict, total=False):
    incident_id: str
    created_at: str
    alert: dict[str, Any]
    target: dict[str, Any]          # namespace, workload, kind, gitops_managed
    status: str                     # ignored|collecting|awaiting_approval|closed
    evidence: dict[str, Any]        # k8s, metrics, logs, errors
    runbook: dict[str, Any]
    classification: dict[str, Any]
    diagnosis: dict[str, Any]
    proposal: dict[str, Any]
    policy: dict[str, Any]
    approval: dict[str, Any]
    execution: dict[str, Any]
    verification: dict[str, Any]
    outcome: str
    report_path: str
    timings: dict[str, Any]
