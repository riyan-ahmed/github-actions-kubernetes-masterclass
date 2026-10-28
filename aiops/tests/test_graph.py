"""End-to-end LangGraph runs against fake Kubernetes/Prometheus/Loki APIs."""
import json

import pytest

from aiops.agent.graph import resume_incident, run_incident
from aiops.agent.llm import DeterministicReasoner
from aiops.tests.conftest import alert
from aiops.tests import fakes


def events(deps, iid):
    return [e["event"] for e in deps.audit.events(iid)]


def test_diagnose_mode_is_read_only(make_deps):
    deps, graph, apps = make_deps("crash", prom_crash=True, log_lines=["listen tcp: address notaport: unknown port"])
    st = run_incident(graph, alert())
    assert st["status"] == "closed" and st["outcome"].startswith("diagnosis only")
    assert st["classification"]["incident_type"] == "crash_loop"
    assert st["proposal"]["action_id"] == "rollout_undo"
    assert st["policy"]["decision"] == "advisory_only" and st["policy"]["would_be"] == "requires_approval"
    assert apps.patches == []
    assert st["runbook"]["doc"] == "crashloopbackoff.md"
    assert st["evidence"]["logs"]["lines"]                             # from Loki mock
    ev = events(deps, st["incident_id"])
    assert ev[0] == "alert_received" and ev[-1] == "incident_closed"
    assert {"evidence_collected", "diagnosis", "action_proposed", "policy_decision"} <= set(ev)
    assert deps.audit.verify_chain()[0]


def test_report_contains_required_sections(make_deps):
    deps, graph, _ = make_deps("oom", prom_oom=True)
    st = run_incident(graph, alert(name="SkillPulseOOMKilled"))
    md = open(st["report_path"]).read()
    for needle in ("Supporting evidence", "OOMKilled", "memory_limit_reduced", "oomkilled.md", "Policy decision",
                   "Approval status", "Action taken", "Verification", "Audit trail", "no MTTR claim",
                   "backend.memory_limit: 128Mi -> 10Mi", "SkillPulseOOMKilled"):
        assert needle in md, needle
    assert json.load(open(st["report_path"].replace("reports", "incidents").replace(".md", ".json")))["incident_id"]


def test_pod_alert_resolves_to_deployment(make_deps):
    deps, graph, _ = make_deps("readiness")
    st = run_incident(graph, alert(workload=None, pod=f"{fakes.DEP}-rev2-abcde"))
    assert st["target"]["name"] == fakes.DEP and st["classification"]["incident_type"] == "readiness_failure"


def test_approval_flow_dry_run_changes_nothing(make_deps):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=True, prom_crash=True)
    st = run_incident(graph, alert())
    assert st["_pending"] and st["status"] == "awaiting_approval" and st["approval"]["status"] == "pending"
    assert apps.patches == []
    final = resume_incident(graph, st["incident_id"], "approve", "alice")
    assert final["execution"]["status"] == "dry_run"
    assert [p["dry_run"] for p in apps.patches] == ["All"]
    assert final["verification"]["status"] == "skipped"
    assert final["outcome"] == "dry-run only: no change made"
    assert not apps.fixed


def test_approved_live_rollback_is_executed_and_verified(make_deps):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True)
    st = run_incident(graph, alert())
    final = resume_incident(graph, st["incident_id"], "approve", "alice")
    assert final["execution"]["status"] == "executed"
    [patch] = apps.patches
    assert patch["name"] == "backend-sim" and patch["ns"] == "skillpulse-stg" and patch["dry_run"] is None
    [op] = patch["body"]
    assert op["op"] == "replace" and op["path"] == "/spec/template"
    assert op["value"]["spec"]["containers"][0]["env"][0]["value"] == "8080"      # revision 1 template
    assert final["execution"]["detail"]["from_revision"] == 2 and final["execution"]["detail"]["to_revision"] == 1
    assert final["verification"]["status"] == "recovered"
    assert final["outcome"] == "action executed and recovery verified"
    ev = events(deps, st["incident_id"])
    for e in ("approval_requested", "approval_approved", "action_executed", "recovery_verification", "incident_closed"):
        assert e in ev
    assert final["approval"]["approver"] == "alice"


def test_rejection_executes_nothing(make_deps):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True)
    st = run_incident(graph, alert())
    final = resume_incident(graph, st["incident_id"], "reject", "bob")
    assert apps.patches == [] and final["outcome"] == "remediation rejected by human"
    assert "approval_rejected" in events(deps, st["incident_id"])


def test_approval_without_approver_is_a_rejection(make_deps):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True)
    st = run_incident(graph, alert())
    resume_incident(graph, st["incident_id"], "approve", "")
    assert apps.patches == []


def test_cannot_resume_twice(make_deps):
    deps, graph, _ = make_deps("crash", mode="approval", prom_crash=True)
    st = run_incident(graph, alert())
    resume_incident(graph, st["incident_id"], "approve", "alice")
    with pytest.raises(ValueError):
        resume_incident(graph, st["incident_id"], "approve", "alice")


def test_rollback_refused_when_previous_revision_not_proven_healthy(make_deps):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prev_ready=False, prom_crash=True)
    st = run_incident(graph, alert())
    assert st["proposal"]["action_id"] == "escalate_human"
    assert st["policy"]["decision"] == "advisory_only"
    assert apps.patches == []


def test_real_backend_deployment_is_never_modified(make_deps, monkeypatch):
    """The existing `backend` deployment is read-only for the agent, even when approved."""
    import aiops.tests.fakes as f
    monkeypatch.setattr(f, "DEP", "backend")
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True)
    # point the fake at a deployment called backend
    apps.read_namespaced_deployment = lambda name, ns: _rename(apps._dep(), "backend")
    st = run_incident(graph, alert(workload="backend"))
    assert st["policy"]["decision"] == "denied"
    assert not st["_pending"] and apps.patches == []
    assert "read-only" in st["policy"]["reasons"][0]


def _rename(dep, name):
    dep.metadata.name = name
    return dep


def test_argo_managed_workload_not_modified(make_deps):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True,
                                  labels={"argocd.argoproj.io/instance": "skillpulse-stg"})
    st = run_incident(graph, alert())
    assert st["policy"]["decision"] == "denied" and "Argo CD" in st["policy"]["reasons"][0]
    assert apps.patches == []


def test_unmonitored_namespace_and_resolved_alerts_are_ignored(make_deps):
    deps, graph, apps = make_deps("crash")
    st = run_incident(graph, alert(ns="kube-system"))
    assert st["status"] == "ignored" and "not monitored" in st["outcome"]
    st = run_incident(graph, alert(status="resolved"))
    assert st["status"] == "ignored" and "resolved" in st["outcome"]
    st = run_incident(graph, alert(workload="ghost"))
    assert st["status"] == "ignored" and "not found" in st["outcome"]


def test_loki_down_degrades_to_kubernetes_logs(make_deps):
    deps, graph, _ = make_deps("crash", loki_status=503, prom_crash=True)
    st = run_incident(graph, alert())
    assert any("loki" in e for e in st["evidence"]["errors"])
    assert st["classification"]["root_cause"] == "invalid_configuration"      # from k8s previous logs
    assert "Kubernetes API (previous container logs)" in open(st["report_path"]).read()


def test_prometheus_down_degrades_gracefully(make_deps):
    deps, graph, _ = make_deps("crash")
    deps.prom = None
    st = run_incident(graph, alert())
    assert st["classification"]["incident_type"] == "crash_loop"
    assert any("prometheus" in e for e in st["evidence"]["errors"])
    assert st["proposal"]["action_id"] == "escalate_human"      # rollback needs proven-healthy history


class LyingLLM:
    name = "lying"

    def explain(self, c, e):
        return {"source": "llm", "incident_type": "oom_killed", "explanation": "it is OOM", "confidence": 0.9}


class BrokenLLM:
    name = "broken"

    def explain(self, c, e):
        raise RuntimeError("401 invalid api key sk-" + "x" * 30)


def test_llm_disagreement_forces_approval(make_deps):
    deps, graph, apps = make_deps("crash", mode="auto", dry_run=False, prom_crash=True, reasoner=LyingLLM())
    st = run_incident(graph, alert())
    assert st["diagnosis"]["disagreement"] and st["classification"]["incident_type"] == "crash_loop"
    assert st["policy"]["decision"] == "requires_approval" and apps.patches == []


def test_llm_failure_falls_back_to_rules_and_redacts(make_deps):
    deps, graph, _ = make_deps("crash", prom_crash=True, reasoner=BrokenLLM())
    st = run_incident(graph, alert())
    assert st["diagnosis"]["source"] == "rules" and st["diagnosis"]["llm_error"]
    raw = deps.audit.path.read_text()
    assert "x" * 30 not in raw


def test_auto_mode_blocked_by_default_policy(make_deps):
    deps, graph, apps = make_deps("crash", mode="auto", dry_run=False, prom_crash=True)
    st = run_incident(graph, alert())
    assert st["policy"]["decision"] == "requires_approval" and apps.patches == []


def test_unclassified_incident_gets_no_runbook_and_a_clean_report(make_deps):
    """Found on the live cluster: a healthy pod with one old restart was matched to the CrashLoop runbook."""
    deps, graph, apps = make_deps("healthy")
    st = run_incident(graph, alert())
    assert st["classification"]["incident_type"] == "unclassified"
    assert st["runbook"] is None and st["proposal"]["action_id"] == "escalate_human"
    md = open(st["report_path"]).read()
    assert "none (incident not classified" in md and "would be `None`" not in md
    assert apps.patches == []


def test_report_shows_the_rollback_target_proof(make_deps):
    """Found on the live crash-loop run: the proof printed as 'previous_revision: null'."""
    deps, graph, _ = make_deps("crash", prom_crash=True)
    md = open(run_incident(graph, alert())["report_path"]).read()
    assert "rollback target `backend-sim-rev1`" in md and "**True**" in md and "-> null" not in md
