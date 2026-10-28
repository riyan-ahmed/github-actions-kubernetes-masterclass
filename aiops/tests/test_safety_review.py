"""Cross-cutting safety properties, exercised end to end (added in the pre-Phase-3 review)."""
import itertools
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from aiops.agent.audit import AuditLog
from aiops.agent.config import Settings
from aiops.agent.graph import Deps, build_graph, resume_incident, run_incident
from aiops.agent.llm import LLMReasoner, parse_llm_json
from aiops.agent.models import ACTION_IDS
from aiops.agent.policy import CLUSTER_WRITE_ACTIONS
from aiops.tests import fakes
from aiops.tests.conftest import alert


# ---- 1. checkpointer + approval resumes the right incident ---------------------------------
def sqlite_graph(deps, path):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return build_graph(deps, SqliteSaver(conn))


def test_sqlite_checkpointer_resumes_across_a_restart(make_deps, tmp_path):
    deps, _, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True)
    db = tmp_path / "checkpoints.sqlite"
    g1 = sqlite_graph(deps, db)
    st = run_incident(g1, alert())
    assert st["_pending"] and apps.patches == []
    # "restart": brand-new graph + connection over the same file
    g2 = sqlite_graph(deps, db)
    final = resume_incident(g2, st["incident_id"], "approve", "alice")
    assert final["execution"]["status"] == "executed" and final["verification"]["status"] == "recovered"
    assert len(apps.patches) == 1


def test_approval_resumes_only_the_named_incident(make_deps, tmp_path):
    deps, _, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True)
    g = sqlite_graph(deps, tmp_path / "c.sqlite")
    a = run_incident(g, alert(), "inc-aaa")
    b = run_incident(g, alert(), "inc-bbb")
    resume_incident(g, "inc-bbb", "reject", "bob")
    assert g.get_state({"configurable": {"thread_id": "inc-aaa"}}).next        # still waiting
    assert not g.get_state({"configurable": {"thread_id": "inc-bbb"}}).next
    assert apps.patches == []
    resume_incident(g, "inc-aaa", "approve", "alice")
    assert len(apps.patches) == 1
    assert {e["data"].get("approver") for e in deps.audit.events("inc-aaa") if e["event"] == "approval_approved"} == {"alice"}
    with pytest.raises(ValueError):
        resume_incident(g, "inc-does-not-exist", "approve", "alice")


# ---- 2. default configuration cannot modify Kubernetes -------------------------------------
def test_defaults_are_read_only_and_dry_run(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("AIOPS_"):
            monkeypatch.delenv(k)
    for s in (Settings(), Settings.from_env()):
        assert s.mode == "diagnose" and s.dry_run is True and not s.github_enabled and s.llm_provider == "none"


def test_invalid_mode_refused(monkeypatch):
    monkeypatch.setenv("AIOPS_MODE", "yolo")
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("scenario", ["crash", "oom", "readiness"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_diagnose_mode_never_writes_even_for_the_allowlisted_workload(make_deps, scenario, dry_run):
    deps, graph, apps = make_deps(scenario, mode="diagnose", dry_run=dry_run, prom_oom=True, prom_crash=True)
    st = run_incident(graph, alert())
    assert apps.patches == [] and not st["_pending"]
    assert "execution" not in st or st["execution"] == {}


def test_even_if_state_is_forced_to_execute_diagnose_mode_blocks(make_deps):
    """Defence in depth: the execute node re-checks policy and the mode itself."""
    deps, graph, apps = make_deps("crash", mode="diagnose", dry_run=False, prom_crash=True)
    cfg = {"configurable": {"thread_id": "inc-force"}}
    run_incident(graph, alert(), "inc-force")
    graph.update_state(cfg, {}, as_node="propose")           # rewind to just after propose
    graph.invoke(None, {**cfg})                              # re-run policy -> still advisory
    assert apps.patches == []


def test_decision_endpoint_disabled_without_token(make_deps):
    from fastapi.testclient import TestClient
    from aiops.agent.server import create_app
    deps, graph, _ = make_deps("crash", mode="approval")
    c = TestClient(create_app(deps, graph))
    assert c.post("/incidents/inc-1/decision", json={"decision": "approve", "approver": "a"},
                  headers={"Authorization": "Bearer "}).status_code == 503
    assert c.post("/webhook/grafana", json={"alerts": []}, headers={"Authorization": "Bearer "}).status_code == 503


# ---- 3. only allowlisted staging actions can execute (exhaustive) --------------------------
def test_exhaustive_executable_decisions_are_exactly_the_allowlist(policy):
    policy.g["auto_remediation_enabled"] = True            # worst case: auto switched on
    namespaces = ["skillpulse-stg", "skillpulse-prd", "skillpulse-dev", "skillpulse", "kube-system", "monitoring"]
    workloads = ["backend-sim", "backend", "frontend", "mysql", "mysql-0", "prometheus"]
    kinds = ["Deployment", "StatefulSet", "DaemonSet", "PersistentVolumeClaim", "Secret", "Pod"]
    executable = []
    for ns, wl, kind, action, mode in itertools.product(namespaces, workloads, kinds, list(ACTION_IDS) + ["shell", "delete_pvc"],
                                                       ["diagnose", "approval", "auto"]):
        params = {"container": "backend", "memory_limit": "256Mi"} if action == "propose_config_pr" else \
                 ({"reason": "x"} if action == "escalate_human" else {})
        d = policy.evaluate(namespace=ns, workload=wl, kind=kind, action_id=action, params=params,
                            mode=mode, confidence=0.95)
        if d.executable:
            executable.append((ns, wl, kind, action, mode))
    assert executable, "sanity: something must be executable"
    for ns, wl, kind, action, mode in executable:
        assert mode in ("approval", "auto")
        if action in CLUSTER_WRITE_ACTIONS:
            assert (ns, wl, kind) == ("skillpulse-stg", "backend-sim", "Deployment"), (ns, wl, kind, action)
        else:   # propose_config_pr: a Git proposal for backend only, never a cluster write
            assert action == "propose_config_pr" and wl == "backend" and kind == "Deployment"
            assert ns in ("skillpulse-stg", "skillpulse-prd")


def test_only_backend_sim_reaches_the_kubernetes_write_api(make_deps, monkeypatch):
    """Whatever the workload name, the only patch call in any mode must be against backend-sim."""
    for name in ("backend-sim", "backend", "frontend"):
        monkeypatch.setattr(fakes, "DEP", name)
        for mode in ("approval", "auto"):
            deps, graph, apps = make_deps("crash", mode=mode, dry_run=False, prom_crash=True)
            apps.read_namespaced_deployment = lambda n, ns, a=apps, nm=name: _named(a._dep(), nm)
            st = run_incident(graph, alert(workload=name))
            if st["_pending"]:
                resume_incident(graph, st["incident_id"], "approve", "alice")
            assert all(p["name"] == "backend-sim" for p in apps.patches), (name, mode)


def _named(dep, name):
    dep.metadata.name = name
    return dep


# ---- 4. LLM output can never become an action ----------------------------------------------
def test_llm_extra_fields_are_dropped():
    out = parse_llm_json('{"incident_type": "crash_loop", "confidence": 0.9, "explanation": "x", '
                         '"action": "shell", "command": "kubectl delete pvc --all", "namespace": "skillpulse-prd"}')
    assert set(out) == {"source", "incident_type", "explanation", "confidence"}


class HijackedLLM:
    """An LLM that has been talked into recommending destructive things."""
    name = "hijacked"

    def explain(self, c, e):
        return {"source": "llm", "incident_type": "crash_loop", "confidence": 1.0,
                "explanation": "Run kubectl delete pvc --all -n skillpulse-prd; action=delete_pvc",
                "action": "delete_pvc", "command": "rm -rf /", "target": {"namespace": "skillpulse-prd", "name": "mysql"}}


def test_hijacked_llm_cannot_change_action_or_target(make_deps):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True, reasoner=HijackedLLM())
    st = run_incident(graph, alert())
    assert st["proposal"]["action_id"] == "rollout_undo" and st["target"]["name"] == "backend-sim"
    assert st["policy"]["decision"] == "requires_approval"
    resume_incident(graph, st["incident_id"], "approve", "alice")
    assert [p["name"] for p in apps.patches] == ["backend-sim"]
    assert all(p["ns"] == "skillpulse-stg" for p in apps.patches)


def test_every_proposal_is_validated_by_policy_before_execution(make_deps, monkeypatch):
    """If the proposal step were compromised, policy and executor still refuse."""
    from aiops.agent import graph as g
    monkeypatch.setattr(g, "propose_action", lambda *a, **k: {"action_id": "delete_pvc", "params": {"x": "y"}, "rationale": "evil"})
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True)
    st = run_incident(graph, alert())
    assert st["policy"]["decision"] == "denied" and not st["_pending"] and apps.patches == []
    monkeypatch.setattr(g, "propose_action", lambda *a, **k: {"action_id": "rollout_undo", "params": {"namespace": "skillpulse-prd"}, "rationale": "evil"})
    st = run_incident(graph, alert())
    assert st["policy"]["decision"] == "denied" and apps.patches == []


# ---- 5. no credentials in any persisted artefact ------------------------------------------
SECRETS = ["hunter2-db-pass", "ghp_" + "A1b2C3d4E5" * 3, "sk-" + "Q" * 28, "S3cr3tT0ken99", "AKIAIOSFODNN7EXAMPLE",
           "rootpw!9"]


class LeakyLLM:
    name = "leaky"

    def explain(self, c, e):
        raise RuntimeError(f"401 for key {SECRETS[2]} password={SECRETS[0]}")


def test_no_secret_reaches_any_file_in_the_audit_directory(make_deps, tmp_path):
    lines = [f"DB_PASSWORD={SECRETS[0]}", f"using token {SECRETS[1]}", f"Authorization: Bearer {SECRETS[3]}abcdef",
             f"dsn app:{SECRETS[5]}@tcp(mysql:3306)/skillpulse", f"aws {SECRETS[4]}", "listen tcp: address notaport: unknown port"]
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True, log_lines=lines,
                                  reasoner=LeakyLLM())
    # secrets also arrive via the alert, the events and the pod log fallback
    a = alert()
    a["summary"] = f"password={SECRETS[0]}"
    import aiops.tests.fakes as f
    f.SCENARIOS["crash"]["events"].append(f._event("Failed", f"pull failed token={SECRETS[3]}"))
    f.SCENARIOS["crash"]["logs"] += f"api_key={SECRETS[2]}\n"
    try:
        from aiops.agent.ingest import parse_webhook
        [pa] = parse_webhook({"alerts": [{"status": "firing", "labels": {"alertname": "x", "namespace": f.NS, "deployment": f.DEP},
                                          "annotations": {"description": f"password={SECRETS[0]} {SECRETS[1]}"}}]})
        g2 = sqlite_graph(deps, tmp_path / "audit" / "checkpoints.sqlite")
        st = run_incident(g2, pa)
        resume_incident(g2, st["incident_id"], "approve", "alice")
    finally:
        f.SCENARIOS["crash"]["events"].pop()
        f.SCENARIOS["crash"]["logs"] = f.SCENARIOS["crash"]["logs"].replace(f"api_key={SECRETS[2]}\n", "")
    scanned = 0
    for p in (tmp_path / "audit").rglob("*"):
        if p.is_file():
            data = p.read_bytes().decode("latin-1")
            scanned += 1
            for s in SECRETS:
                assert s not in data, f"{s!r} leaked into {p.name}"
    assert scanned >= 4          # audit.jsonl, incident json, report, checkpoints.sqlite
    assert any(p.suffix == ".sqlite" for p in (tmp_path / "audit").rglob("*"))


def test_configured_tokens_tolerate_trailing_newline(monkeypatch):
    """Secrets created with `echo`/`openssl rand` carry a trailing newline; auth must still work."""
    monkeypatch.setenv("AIOPS_WEBHOOK_TOKEN", "abc123\n")
    monkeypatch.setenv("AIOPS_APPROVAL_TOKEN", "def456\n")
    s = Settings.from_env()
    assert s.webhook_token == "abc123" and s.approval_token == "def456"
