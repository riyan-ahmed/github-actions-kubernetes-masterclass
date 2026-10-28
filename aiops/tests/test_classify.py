import pytest

from aiops.agent.classify import classify, propose_action
from aiops.agent.retrieval import RunbookIndex, build_query
from aiops.tests import fakes
from aiops.tests.conftest import ROOT


def evidence(scenario, prev_ready=True, history=True, log_lines=(), **prom):
    from aiops.agent.collectors.prometheus import PrometheusClient
    from aiops.agent.collectors.loki import LokiClient
    kube, _ = fakes.make_kube(scenario, history)
    k8s = kube.collect(fakes.NS, fakes.DEP)
    prom_c = PrometheusClient("http://p", fakes.prom_transport(prev_ready, **prom))
    pods = [p["name"] for p in k8s["pods"]]
    metrics = prom_c.collect(fakes.NS, pods)
    metrics["previous_revision"] = prom_c.was_ready(fakes.NS, k8s["revisions"][1]["name"]) if history else {}
    logs = LokiClient("http://l", fakes.loki_transport(log_lines)).collect(fakes.NS, pods)
    return {"k8s": k8s, "metrics": metrics, "logs": logs, "errors": []}


@pytest.mark.parametrize("scenario,itype,cause,prom", [
    ("crash", "crash_loop", "invalid_configuration", {"crash": True}),
    ("oom", "oom_killed", "memory_limit_reduced", {"oom": True}),
    ("readiness", "readiness_failure", "probe_misconfigured", {}),
])
def test_classifies_each_scenario(scenario, itype, cause, prom):
    c = classify(evidence(scenario, **prom))
    assert (c["incident_type"], c["root_cause"]) == (itype, cause)
    assert c["signals"] and c["confidence"] >= 0.6


def test_healthy_workload_is_unclassified():
    c = classify(evidence("healthy"))
    assert c["incident_type"] == "unclassified" and c["confidence"] < 0.6


def test_oom_takes_priority_over_crashloop():
    # the OOM pod is also in CrashLoopBackOff; the classifier must still say OOM
    assert classify(evidence("oom"))["incident_type"] == "oom_killed"


def test_database_unreachable_is_not_blamed_on_rollout():
    ev = evidence("crash")
    ev["k8s"]["previous_logs"] = {"p/backend": ["Waiting for database... attempt 30/30",
                                                "Could not connect to database: dial tcp: lookup mysqlx"]}
    c = classify(ev)
    assert c["root_cause"] == "database_unreachable"
    assert propose_action(c, ev)["action_id"] == "escalate_human"      # restarts would not help


def test_rollback_proposed_only_with_proven_healthy_previous_revision():
    ev = evidence("crash", prev_ready=True)
    assert propose_action(classify(ev), ev)["action_id"] == "rollout_undo"
    ev = evidence("crash", prev_ready=False)
    p = propose_action(classify(ev), ev)
    assert p["action_id"] == "escalate_human" and "not proven healthy" in p["rationale"]


def test_no_history_means_no_rollback():
    ev = evidence("crash", history=False)
    assert propose_action(classify(ev), ev)["action_id"] == "escalate_human"


def test_oom_without_rollout_change_proposes_bounded_pr():
    ev = evidence("oom")
    ev["k8s"]["revision_diff"] = []
    c = classify(ev)
    assert c["root_cause"] == "memory_limit_exceeded"
    p = propose_action(c, ev)
    assert p["action_id"] == "propose_config_pr" and p["params"] == {"container": "backend", "memory_limit": "20Mi"}


def test_dependency_unavailable_escalates():
    ev = evidence("readiness")
    ev["k8s"]["events"] = [{"type": "Warning", "reason": "Unhealthy", "object": "Pod/x", "count": 1, "last_seen": None,
                            "message": "Readiness probe failed: HTTP probe failed with statuscode: 503"}]
    c = classify(ev)
    assert c["root_cause"] == "dependency_unavailable"
    assert propose_action(c, ev)["action_id"] == "escalate_human"


def test_runbook_is_retrieved_from_evidence_not_alert_name():
    idx = RunbookIndex.from_dir(ROOT / "runbooks")
    for scenario, doc, prom in [("crash", "crashloopbackoff.md", {"crash": True}),
                                ("oom", "oomkilled.md", {"oom": True}),
                                ("readiness", "readiness-failure.md", {})]:
        ev = evidence(scenario, **prom)
        assert idx.best_runbook(build_query(classify(ev), ev))["doc"] == doc
