import pytest

from aiops.agent.policy import Policy, PolicyError, validate_action_request

BASE = dict(namespace="skillpulse-stg", workload="backend-sim", kind="Deployment", action_id="rollout_undo",
            params={}, mode="approval", confidence=0.9)


def ev(policy, **kw):
    return policy.evaluate(**{**BASE, **kw})


def test_default_diagnose_mode_is_advisory_only(policy):
    d = ev(policy, mode="diagnose")
    assert d.decision == "advisory_only" and d.would_be == "requires_approval" and not d.executable


def test_approval_mode_requires_approval_for_rollback(policy):
    d = ev(policy)
    assert d.decision == "requires_approval" and d.executable


def test_auto_mode_is_disabled_by_default_policy(policy):
    d = ev(policy, mode="auto", action_id="restart_deployment")
    assert d.decision == "requires_approval"        # auto_remediation_enabled: false


def test_auto_requires_flag_allowlist_and_agreement(policy):
    policy.g["auto_remediation_enabled"] = True
    assert ev(policy, mode="auto", action_id="restart_deployment").decision == "auto_allowed"
    assert ev(policy, mode="auto", action_id="rollout_undo").decision == "requires_approval"   # approval-required action
    assert ev(policy, mode="auto", action_id="restart_deployment", llm_disagrees=True).decision == "requires_approval"
    assert ev(policy, mode="approval", action_id="restart_deployment").decision == "requires_approval"


@pytest.mark.parametrize("workload", ["backend", "frontend", "mysql"])
def test_existing_workloads_are_read_only(policy, workload):
    kind = "StatefulSet" if workload == "mysql" else "Deployment"
    d = ev(policy, workload=workload, kind=kind)
    assert d.decision == "denied"


def test_statefulset_pvc_secret_kinds_forbidden(policy):
    for kind in ("StatefulSet", "PersistentVolumeClaim", "Secret"):
        assert ev(policy, kind=kind).decision == "denied"


@pytest.mark.parametrize("action", ["rollout_undo", "restart_deployment"])
def test_production_is_never_written(policy, action):
    d = ev(policy, namespace="skillpulse-prd", workload="backend", action_id=action)
    assert d.decision == "denied" and "not allowed in production" in d.reasons[0]


def test_production_config_change_is_pr_with_approval_only(policy):
    d = ev(policy, namespace="skillpulse-prd", workload="backend", action_id="propose_config_pr",
           params={"container": "backend", "memory_limit": "256Mi"})
    assert d.decision == "requires_approval"


def test_pr_for_workload_without_gitops_target_denied(policy):
    d = ev(policy, action_id="propose_config_pr", params={"container": "backend", "memory_limit": "256Mi"})
    assert d.decision == "denied" and "no GitOps target" in d.reasons[0]


def test_unmonitored_namespace_denied(policy):
    assert ev(policy, namespace="kube-system").decision == "denied"
    assert ev(policy, namespace="skillpulse-dev").decision == "denied"


def test_argo_managed_workload_must_change_through_git(policy):
    d = ev(policy, gitops_managed=True)
    assert d.decision == "denied" and "Argo CD" in d.reasons[0]


def test_rate_limit(policy):
    assert ev(policy, recent_executions=2).decision == "denied"
    assert ev(policy, recent_executions=1).decision == "requires_approval"


def test_low_confidence_is_advisory(policy):
    assert ev(policy, confidence=0.3).decision == "advisory_only"


def test_escalation_is_always_advisory(policy):
    d = ev(policy, action_id="escalate_human", params={"reason": "look at mysql"})
    assert d.decision == "advisory_only" and not d.executable


def test_llm_disagreement_forces_approval_even_in_auto(policy):
    policy.g["auto_remediation_enabled"] = True
    assert ev(policy, mode="auto", action_id="restart_deployment", llm_disagrees=True).decision == "requires_approval"


def test_policy_version_checked():
    with pytest.raises(PolicyError):
        Policy({"version": 2})
