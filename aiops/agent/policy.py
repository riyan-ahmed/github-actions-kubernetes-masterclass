"""Deterministic action validation and policy evaluation.

An LLM (or an attacker controlling logs/alerts) can at most *suggest* an action id and
parameters. Everything here is plain code: unknown actions, unexpected parameters,
unlisted workloads, forbidden kinds and production targets are rejected before anything
can reach the Kubernetes API.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import ACTION_IDS
from .util import parse_quantity, valid_name

CLUSTER_WRITE_ACTIONS = ("rollout_undo", "restart_deployment")
_SHELL_META = re.compile(r"[;&|`$<>\\\n\r(){}*!]")


class PolicyError(ValueError):
    pass


def validate_action_request(action_id: object, params: object, memory_cap: str = "512Mi") -> dict:
    """Validate an action proposal against the closed registry. Raises PolicyError."""
    if not isinstance(action_id, str) or action_id not in ACTION_IDS:
        raise PolicyError(f"action {str(action_id)[:40]!r} is not in the allowlist")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise PolicyError("params must be an object")
    for k, v in params.items():
        # escalate_human's reason is display-only text that is never executed or interpolated
        if action_id == "escalate_human" and k == "reason":
            continue
        if isinstance(v, str) and _SHELL_META.search(v):
            raise PolicyError(f"parameter {k!r} contains shell metacharacters")

    if action_id in CLUSTER_WRITE_ACTIONS:
        # Target namespace/workload come from the incident, never from parameters.
        if params:
            raise PolicyError(f"{action_id} takes no parameters (got {sorted(params)})")
        return {}
    if action_id == "propose_config_pr":
        if set(params) != {"container", "memory_limit"}:
            raise PolicyError("propose_config_pr requires exactly container and memory_limit")
        if not valid_name(params["container"]):
            raise PolicyError("invalid container name")
        try:
            limit = parse_quantity(params["memory_limit"])
        except ValueError as e:
            raise PolicyError(str(e)) from e
        if limit > parse_quantity(memory_cap):
            raise PolicyError(f"memory_limit exceeds cap {memory_cap}")
        return {"container": params["container"], "memory_limit": params["memory_limit"]}
    # escalate_human
    if set(params) - {"reason"}:
        raise PolicyError("escalate_human only accepts 'reason'")
    return {"reason": str(params.get("reason", ""))[:300]}


@dataclass
class PolicyDecision:
    decision: str                        # advisory_only|requires_approval|auto_allowed|denied
    reasons: list[str] = field(default_factory=list)
    would_be: str | None = None          # decision if the mode allowed execution (diagnose mode)

    def as_dict(self) -> dict:
        return {"decision": self.decision, "reasons": self.reasons, "would_be": self.would_be}

    @property
    def executable(self) -> bool:
        return self.decision in ("requires_approval", "auto_allowed")


class Policy:
    def __init__(self, data: dict):
        if data.get("version") != 1:
            raise PolicyError("unsupported policy version")
        self.data = data
        self.g = data.get("global", {})
        self.envs = data.get("environments", {})
        self.gitops = data.get("gitops_targets", {})

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls(yaml.safe_load(Path(path).read_text()))

    @property
    def memory_cap(self) -> str:
        return self.g.get("memory_limit_cap", "512Mi")

    def monitored(self, namespace: str) -> bool:
        return namespace in self.envs

    def evaluate(self, *, namespace: str, workload: str, kind: str, action_id: str, params: dict | None,
                 mode: str, confidence: float, llm_disagrees: bool = False,
                 gitops_managed: bool = False, recent_executions: int = 0) -> PolicyDecision:
        deny = lambda why: PolicyDecision("denied", [why])  # noqa: E731
        try:
            validate_action_request(action_id, params, self.memory_cap)
        except PolicyError as e:
            return deny(f"invalid action request: {e}")

        env = self.envs.get(namespace)
        if env is None:
            return deny(f"namespace {namespace!r} is not managed by policy")
        if kind in self.g.get("forbidden_kinds", []):
            return deny(f"kind {kind} is forbidden")
        if kind not in self.g.get("supported_kinds", ["Deployment"]):      # default-deny
            return deny(f"kind {kind} is not a supported remediation target")
        if action_id not in env.get("allowed_actions", []):
            return deny(f"action {action_id} not allowed in {env.get('tier')} namespace {namespace}")

        reasons: list[str] = []
        if action_id == "escalate_human":
            return PolicyDecision("advisory_only", ["escalation has no side effects; a human must act"])

        if action_id in CLUSTER_WRITE_ACTIONS:
            allowed = {(w["kind"], w["name"]) for w in env.get("write_allowlist", [])}
            if (kind, workload) not in allowed:
                return deny(f"{kind}/{workload} in {namespace} is read-only for the agent (not in write_allowlist)")
            if gitops_managed:
                return deny("workload is managed by Argo CD; change it through Git, not the cluster")
            if recent_executions >= self.g.get("max_executions_per_workload_per_hour", 2):
                return deny("rate limit: too many recent executions against this workload")
        elif action_id == "propose_config_pr":
            if workload not in self.gitops.get(namespace, {}):
                return deny(f"no GitOps target configured for {workload} in {namespace}")

        if confidence < self.g.get("min_confidence_for_execution", 0.6):
            return PolicyDecision("advisory_only", [f"confidence {confidence:.2f} below execution threshold"])

        needs_approval = action_id in env.get("approval_required_actions", [])
        auto_ok = (
            action_id in env.get("auto_actions", [])
            and self.g.get("auto_remediation_enabled", False)
            and not llm_disagrees
        )
        if llm_disagrees:
            reasons.append("LLM and rules disagree; approval required")
            needs_approval = True

        if auto_ok and not needs_approval:
            outcome, why = "auto_allowed", "allowlisted staging action eligible for automatic execution"
        else:
            outcome, why = "requires_approval", "explicit human approval required"
        reasons.append(why)

        if mode == "diagnose":
            return PolicyDecision("advisory_only", reasons + ["mode=diagnose: read-only, nothing will be executed"], would_be=outcome)
        if mode == "approval" and outcome == "auto_allowed":
            return PolicyDecision("requires_approval", reasons + ["mode=approval: automatic execution disabled"])
        if mode == "auto" and outcome == "auto_allowed" and not self.g.get("auto_remediation_enabled", False):
            return PolicyDecision("requires_approval", reasons + ["auto_remediation_enabled is false"])
        return PolicyDecision(outcome, reasons)

    def gitops_target(self, namespace: str, workload: str) -> dict | None:
        return self.gitops.get(namespace, {}).get(workload)
