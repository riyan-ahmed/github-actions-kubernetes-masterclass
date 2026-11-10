"""GitOps pull-request remediation of the isolated oom-demo workload.

Covers: the policy scope (PR only, never a cluster write), the pull-request scope (one allow-listed file, limits only),
the post-merge tracker (human merge -> Argo CD sync -> healthy workload -> audit timeline), the end-to-end graph,
and the manifests (read-only RBAC, automatic-sync-without-prune Application, isolated project).
"""
import base64
import copy
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from aiops.agent.audit import AuditLog
from aiops.agent.collectors.argocd import summarize_application
from aiops.agent.config import Settings
from aiops.agent.gitops import GitHubPR, check_pr_path, edit_memory_limit, propose_config_change
from aiops.agent.gitops_verify import PRTracker, build_timeline, rebuild_tracking_inputs
from aiops.agent.graph import resume_incident, run_incident
from aiops.agent.policy import Policy, PolicyError, validate_action_request
from aiops.tests.conftest import ROOT, alert
from aiops.tests import fakes

REPO = ROOT.parent
NS, APP = "skillpulse-gitops", "oom-demo"
PARAMS = {"container": "oom-demo", "memory_limit": "64Mi"}
REAL_FILE = (REPO / "k8s/gitops/oom-demo/patches/resources.yaml").read_text()


def contents_response(text):
    return httpx.Response(200, json={"sha": "filesha", "content": base64.b64encode(text.encode()).decode()})
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*", "escalate", "bind", "impersonate"}
NEEDS_KUBECTL = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not available")


def decide(policy, action="propose_config_pr", params=PARAMS, workload=APP, mode="approval", confidence=0.9, **kw):
    return policy.evaluate(namespace=NS, workload=workload, kind="Deployment", action_id=action, params=params,
                           mode=mode, confidence=confidence, **kw)


# ---- policy: the agent can open a PR, and nothing else, in the GitOps namespace -------------------------
def test_pr_for_oom_demo_is_allowed_without_pre_approval_because_the_merge_is_the_approval(policy):
    d = decide(policy)
    assert d.decision == "pr_allowed" and d.executable
    assert "human must review and merge" in " ".join(d.reasons)


def test_diagnose_mode_never_opens_a_pr(policy):
    d = decide(policy, mode="diagnose")
    assert d.decision == "advisory_only" and d.would_be == "pr_allowed" and not d.executable


@pytest.mark.parametrize("action", ["rollout_undo", "restart_deployment"])
@pytest.mark.parametrize("managed", [True, False])
def test_every_cluster_write_is_denied_for_the_gitops_workload(policy, action, managed):
    d = decide(policy, action=action, params={}, gitops_managed=managed)
    assert d.decision == "denied" and not d.executable


def test_gitops_namespace_has_an_empty_write_allowlist_and_no_auto_actions(policy):
    env = policy.envs[NS]
    assert env["write_allowlist"] == [] and env["auto_actions"] == []
    assert set(env["allowed_actions"]) == {"propose_config_pr", "escalate_human"}
    assert policy.g["auto_remediation_enabled"] is False


@pytest.mark.parametrize("workload", ["backend", "frontend", "mysql", "other"])
def test_other_workloads_in_the_sandbox_get_no_pr(policy, workload):
    assert decide(policy, workload=workload).decision == "denied"


def test_memory_cap_is_enforced(policy):
    assert decide(policy, params={"container": "oom-demo", "memory_limit": "513Mi"}).decision == "denied"
    assert decide(policy, params={"container": "oom-demo", "memory_limit": "512Mi"}).decision == "pr_allowed"
    with pytest.raises(PolicyError):
        validate_action_request("propose_config_pr", {"container": "oom-demo", "memory_limit": "1Gi"}, "512Mi")


def test_low_confidence_gets_no_pr(policy):
    assert decide(policy, confidence=0.4).decision == "advisory_only"


def test_llm_disagreement_demotes_the_pr_to_an_approval(policy):
    assert decide(policy, llm_disagrees=True).decision == "requires_approval"


def test_pr_without_preapproval_is_not_granted_to_staging_or_production(policy):
    for ns in ("skillpulse-stg", "skillpulse-prd"):
        assert "pr_without_preapproval" not in policy.envs[ns]
    d = policy.evaluate(namespace="skillpulse-prd", workload="backend", kind="Deployment", action_id="propose_config_pr",
                        params={"container": "backend", "memory_limit": "256Mi"}, mode="approval", confidence=0.9)
    assert d.decision == "requires_approval"


# ---- pull-request scope ------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["../etc/passwd.yaml", "/abs/k8s/gitops/x.yaml", "k8s/gitops/../../secrets.yaml",
                                  "k8s/base/20-backend.yaml", "aiops/policies/policy.yaml", ".github/workflows/ci.yaml",
                                  "k8s/gitops/oom-demo/patches/resources.sh", "k8s//gitops/x.yaml"])
def test_pr_path_outside_scope_is_refused(path):
    with pytest.raises(ValueError):
        check_pr_path(path, ["k8s/gitops/"])


def test_pr_path_inside_scope_is_accepted():
    assert check_pr_path("k8s/gitops/oom-demo/patches/resources.yaml", ["k8s/gitops/"])


def test_a_policy_target_outside_the_scope_cannot_produce_a_pr(policy, tmp_path):
    bad = copy.deepcopy(policy.data)
    bad["gitops_targets"][NS][APP]["path"] = "k8s/base/20-backend.yaml"
    s = Settings(audit_dir=str(tmp_path), github_enabled=True, github_token="t", github_repo="o/r")
    with pytest.raises(ValueError):
        propose_config_change(settings=s, policy=Policy(bad), incident_id="i", namespace=NS, workload=APP,
                              params=PARAMS, rationale="", dry_run=False, transport=httpx.MockTransport(lambda r: httpx.Response(500)))


def github(tmp_path, handler):
    s = Settings(audit_dir=str(tmp_path), github_enabled=True, github_token="t", github_repo="o/r", github_base_branch="gitops-demo")
    return s, httpx.MockTransport(handler)


def test_pr_changes_exactly_one_file_and_only_limits_memory(policy, tmp_path):
    puts, bodies = [], []

    def handler(req):
        p = req.url.path
        if req.method == "GET" and p.endswith("/git/ref/heads/gitops-demo"):
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if req.method == "POST" and p.endswith("/git/refs"):
            assert json.loads(req.content)["ref"] == "refs/heads/aiops/inc-9"
            return httpx.Response(201, json={})
        if req.method == "GET" and "/contents/" in p:
            return contents_response(REAL_FILE)
        if req.method == "PUT":
            puts.append(p)
            body = json.loads(req.content)
            assert body["branch"] == "aiops/inc-9"
            new = base64.b64decode(body["content"]).decode()
            doc = yaml.safe_load(new)
            res = doc["spec"]["template"]["spec"]["containers"][0]["resources"]
            assert res == {"limits": {"memory": "64Mi"}}, "requests must not be touched"
            import difflib
            changed = [l for l in difflib.unified_diff(REAL_FILE.splitlines(), new.splitlines(), lineterm="", n=0)
                       if l[:1] in "+-" and l[:3] not in ("+++", "---")]
            assert changed == ["-              memory: 32Mi", "+              memory: 64Mi"], changed     # ONE line
            return httpx.Response(200, json={})
        if req.method == "POST" and p.endswith("/pulls"):
            j = json.loads(req.content)
            assert j["base"] == "gitops-demo" and j["head"] == "aiops/inc-9"
            assert "Merging it is the human approval" in j["body"] and "syncs automatically (pruning disabled)" in j["body"]
            bodies.append(j)
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7", "number": 7, "created_at": "2026-11-10T10:00:10Z"})
        return httpx.Response(404)
    s, tr = github(tmp_path, handler)
    r = propose_config_change(settings=s, policy=policy, incident_id="inc-9", namespace=NS, workload=APP, params=PARAMS,
                              rationale="why", dry_run=False, transport=tr)
    assert puts == ["/repos/o/r/contents/k8s/gitops/oom-demo/patches/resources.yaml"]
    assert r["mode"] == "pull_request" and r["number"] == 7 and r["argocd_app"] == APP and r["memory_limit"] == "64Mi"


def test_the_agent_never_merges_or_pushes_to_the_base_branch():
    src = (ROOT / "agent" / "gitops.py").read_text()
    assert "/merge" not in src and "auto_merge" not in src and "enable_pull_request_auto_merge" not in src.lower()
    assert 'method="PUT"' not in src and "git/refs/heads" not in src.replace("git/ref/heads", "")


def test_pr_dry_run_is_its_own_switch_and_ignores_the_kubernetes_dry_run(policy, tmp_path):
    import aiops.agent.actions as actions
    from aiops.agent.actions import execute_action
    seen = []
    orig = actions.propose_config_change
    actions.propose_config_change = lambda **kw: seen.append(kw["dry_run"]) or {"mode": "pull_request", "url": "u"}
    try:
        for k8s_dry, pr_dry, github, expect in [(True, False, True, False), (False, True, True, True),
                                                (True, True, True, True), (False, False, False, False)]:
            s = Settings(audit_dir=str(tmp_path), dry_run=k8s_dry, pr_dry_run=pr_dry, github_enabled=github)
            execute_action(action_id="propose_config_pr", params=PARAMS, target={"namespace": NS, "name": APP}, kube=None,
                           prom=None, policy=policy, settings=s, incident_id="i", rationale="r", dry_run=k8s_dry)
            assert seen[-1] is expect, (k8s_dry, pr_dry, github)
    finally:
        actions.propose_config_change = orig


def test_defaults_are_safe_and_env_parsing_is_explicit(monkeypatch):
    s = Settings()
    assert s.pr_dry_run is True and s.dry_run is True and s.github_enabled is False
    monkeypatch.setenv("AIOPS_MODE", "approval")
    monkeypatch.setenv("AIOPS_DRY_RUN", "true")
    monkeypatch.setenv("AIOPS_GITHUB_ENABLED", "true")
    monkeypatch.setenv("AIOPS_PR_DRY_RUN", "false")
    e = Settings.from_env()
    assert e.dry_run is True and e.pr_dry_run is False and e.github_enabled is True
    monkeypatch.delenv("AIOPS_PR_DRY_RUN")
    assert Settings.from_env().pr_dry_run is True


def test_live_pr_with_kubernetes_dry_run_on_opens_a_real_pr_and_never_touches_the_cluster(make_deps, monkeypatch):
    """AIOPS_DRY_RUN=true + AIOPS_PR_DRY_RUN=false: the PR is real, no Kubernetes write of any kind is issued."""
    data = copy.deepcopy(Policy.load(ROOT / "policies" / "policy.yaml").data)
    data["environments"]["skillpulse-stg"]["pr_without_preapproval"] = ["propose_config_pr"]
    data["gitops_targets"]["skillpulse-stg"]["backend-sim"] = {
        "path": "k8s/gitops/oom-demo/patches/resources.yaml", "container": "backend", "patch_requests": False, "argocd_app": APP}
    deps, _, apps = make_deps("oom", mode="approval", dry_run=True, history=False)
    deps.policy = Policy(data)
    deps.settings = Settings(mode="approval", dry_run=True, pr_dry_run=False, github_enabled=True, github_token="t",
                             github_repo="o/r", audit_dir=deps.settings.audit_dir)
    created = []

    def handler(req):
        p = req.url.path
        if req.method == "GET" and "/git/ref/" in p:
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if req.method == "GET" and "/contents/" in p:
            return contents_response(REAL_FILE.replace("        - name: oom-demo", "        - name: backend"))
        if req.method in ("POST", "PUT") and p.endswith(("/git/refs", "/patches/resources.yaml")):
            return httpx.Response(201, json={})
        if req.method == "POST" and p.endswith("/pulls"):
            created.append(1)
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7", "number": 7, "created_at": "2026-11-10T10:00:10Z"})
        return httpx.Response(404)
    import aiops.agent.actions as actions
    real = actions.propose_config_change
    monkeypatch.setattr(actions, "propose_config_change", lambda **kw: real(**kw, transport=httpx.MockTransport(handler)))
    from aiops.agent.graph import build_graph
    st = run_incident(build_graph(deps), alert())
    assert created == [1] and st["execution"]["status"] == "executed" and st["execution"]["detail"]["mode"] == "pull_request"
    assert apps.patches == [], "no Deployment patch (not even a dry-run one) may be issued"
    events = [e["event"] for e in deps.audit.events(st["incident_id"])]
    assert "policy_decision" in events and "action_executed" in events and deps.audit.verify_chain()[0]


@pytest.mark.parametrize("action", ["rollout_undo", "restart_deployment"])
def test_direct_kubernetes_writes_stay_blocked_even_when_prs_are_live(policy, action):
    """pr_dry_run=false must not change what the policy lets the agent do to the cluster."""
    for mode in ("approval", "auto"):
        d = decide(policy, action=action, params={}, mode=mode)
        assert d.decision == "denied" and not d.executable


def test_kubernetes_writes_keep_their_own_dry_run_flag(policy):
    from aiops.agent.actions import execute_action

    class Kube:
        calls = []

        def restart(self, ns, name, ts, dry_run):
            self.calls.append(dry_run)
            return {}

    kube = Kube()
    s = Settings(dry_run=True, pr_dry_run=False, github_enabled=True)
    execute_action(action_id="restart_deployment", params={}, target={"namespace": "skillpulse-stg", "name": "backend-sim"},
                   kube=kube, prom=None, policy=policy, settings=s, incident_id="i", rationale="", dry_run=s.dry_run)
    assert kube.calls == [True], "Kubernetes writes follow AIOPS_DRY_RUN only"


# ---- the tracker: merge -> Argo CD sync -> healthy -> audit timeline -----------------------------------
def snapshot(healthy=True, limit="64Mi"):
    pod = {"name": "oom-demo-abc", "ready": healthy,
           "containers": [{"name": "oom-demo", "ready": healthy, "restart_count": 0,
                           "state": {"kind": "running"} if healthy else {"kind": "waiting", "reason": "CrashLoopBackOff"}}]}
    return {"deployment": {"replicas_desired": 1, "replicas_available": int(healthy), "replicas_updated": 1,
                           "generation": 3, "observed_generation": 3},
            "pods": [pod], "revisions": [{"current": True, "template": {"containers": {"oom-demo": {"memory_limit": limit}}}}]}


class Script:
    """Returns the next scripted value per call, repeating the last."""
    def __init__(self, *values):
        self.values, self.i = list(values), 0

    def __call__(self, *a, **k):
        v = self.values[min(self.i, len(self.values) - 1)]
        self.i += 1
        return v


class FakeGitHub:
    def __init__(self, *states):
        self.next = Script(*states)

    def get_pr(self, number):
        assert number == 7
        return self.next()


class FakeArgo:
    def __init__(self, *states):
        self.next = Script(*states)

    def get(self, app):
        assert app == APP
        return self.next()


class FakeKube:
    def __init__(self, *snaps):
        self.next = Script(*snaps)

    def collect(self, ns, name, log_tail=0):
        assert (ns, name) == (NS, APP)
        return copy.deepcopy(self.next())


OPEN = {"state": "open", "merged": False, "merged_at": None, "merge_commit_sha": None, "closed_at": None, "merged_by": None}
MERGED = {"state": "closed", "merged": True, "merged_at": "2026-11-10T10:05:00Z", "merge_commit_sha": "sha1",
          "closed_at": "2026-11-10T10:05:00Z", "merged_by": "riyan-ahmed"}
CLOSED = {**OPEN, "state": "closed", "closed_at": "2026-11-10T10:05:00Z"}
PENDING = {"sync_status": "OutOfSync", "sync_revision": "old", "health": "Degraded", "operation_phase": "Succeeded",
           "operation_started_at": "x", "operation_finished_at": "x", "operation_revision": "old", "auto_sync": True, "prune": False}
SYNCED = {**PENDING, "sync_status": "Synced", "sync_revision": "sha1", "health": "Healthy", "operation_revision": "sha1",
          "operation_started_at": "2026-11-10T10:05:20Z", "operation_finished_at": "2026-11-10T10:05:24Z"}
DETAIL = {"url": "https://github.com/o/r/pull/7", "number": 7, "created_at": "2026-11-10T10:00:10Z", "branch": "aiops/inc-1",
          "base": "gitops-demo", "memory_limit": "64Mi", "container": "oom-demo", "argocd_app": APP}
TARGET = {"namespace": NS, "name": APP, "kind": "Deployment"}


def tracker(tmp_path, gh, argo, kube, **kw):
    s = Settings(audit_dir=str(tmp_path), pr_wait_timeout_s=30, argocd_wait_timeout_s=30, verify_timeout_s=30, tracker_poll_s=1)
    clock = {"t": 0.0}
    audit = AuditLog(tmp_path)
    return PRTracker(settings=s, audit=audit, kube=kube, argo=argo, github=gh,
                     sleep=lambda x: clock.__setitem__("t", clock["t"] + x), clock=lambda: clock["t"], **kw), audit


def seed_audit(audit, iid="inc-1"):
    audit.record(iid, "alert_received", "aiops-agent", {"alert": {"starts_at": "2026-11-10T09:59:50+00:00"}})
    audit.record(iid, "diagnosis", "aiops-agent", {})
    audit.record(iid, "action_executed", "aiops-agent", {**DETAIL, "mode": "pull_request", "dry_run": False,
                                                          "namespace": NS, "workload": APP})


def test_full_gitops_recovery_is_recorded_in_order(tmp_path):
    t, audit = tracker(tmp_path, FakeGitHub(OPEN, OPEN, MERGED), FakeArgo(PENDING, SYNCED), FakeKube(snapshot(False, "32Mi"), snapshot(True)))
    seed_audit(audit)
    res = t.track("inc-1", TARGET, DETAIL)
    assert res["status"] == "recovered"
    names = [e["event"] for e in audit.events("inc-1")]
    order = ["alert_received", "diagnosis", "action_executed", "recovery_tracking_started", "pr_merged", "argocd_synced",
             "workload_recovered", "gitops_recovery_complete"]
    assert [n for n in names if n in order] == order
    assert audit.verify_chain()[0]
    merged = next(e for e in audit.events("inc-1") if e["event"] == "pr_merged")
    assert merged["data"]["merged_by"] == "riyan-ahmed" and merged["data"]["merge_commit_sha"] == "sha1"
    assert res["timeline"]["stages"]["merge_to_argocd_sync_started_s"] == 20.0
    assert res["timeline"]["stages"]["argocd_sync_duration_s"] == 4.0
    assert res["timeline"]["stages"]["pr_open_to_human_merge_s"] == 290.0


def test_unmerged_closed_pr_is_recorded_and_nothing_else_happens(tmp_path):
    t, audit = tracker(tmp_path, FakeGitHub(OPEN, CLOSED), FakeArgo(SYNCED), FakeKube(snapshot()))
    assert t.track("inc-2", TARGET, DETAIL)["status"] == "pr_rejected"
    assert [e["event"] for e in audit.events("inc-2")] == ["recovery_tracking_started", "pr_closed_unmerged"]


def test_tracker_gives_up_if_nobody_merges(tmp_path):
    t, audit = tracker(tmp_path, FakeGitHub(OPEN), FakeArgo(SYNCED), FakeKube(snapshot()))
    assert t.track("inc-3", TARGET, DETAIL)["status"] == "pr_not_merged"
    assert audit.events("inc-3")[-1]["event"] == "pr_wait_timeout"


def test_argocd_that_never_reaches_the_merge_commit_is_reported(tmp_path):
    t, audit = tracker(tmp_path, FakeGitHub(MERGED), FakeArgo(PENDING), FakeKube(snapshot()))
    assert t.track("inc-4", TARGET, DETAIL)["status"] == "argocd_not_synced"
    assert audit.events("inc-4")[-1]["event"] == "argocd_sync_timeout"


def test_a_sync_of_an_older_revision_does_not_count(tmp_path):
    older = {**SYNCED, "sync_revision": "other", "operation_revision": "other"}
    t, _ = tracker(tmp_path, FakeGitHub(MERGED), FakeArgo(older), FakeKube(snapshot()))
    assert t.track("inc-5", TARGET, DETAIL)["status"] == "argocd_not_synced"


def test_recovery_requires_the_proposed_limit_to_be_live(tmp_path):
    """A healthy pod that still runs the OLD limit is not a recovery caused by the merged change."""
    t, audit = tracker(tmp_path, FakeGitHub(MERGED), FakeArgo(SYNCED), FakeKube(snapshot(True, "32Mi")))
    assert t.track("inc-6", TARGET, DETAIL)["status"] == "not_recovered"
    assert audit.events("inc-6")[-1]["event"] == "recovery_not_verified"


def test_unhealthy_workload_after_sync_is_not_reported_recovered(tmp_path):
    t, _ = tracker(tmp_path, FakeGitHub(MERGED), FakeArgo(SYNCED), FakeKube(snapshot(False, "64Mi")))
    assert t.track("inc-7", TARGET, DETAIL)["status"] == "not_recovered"


def test_tracker_failure_is_audited_not_swallowed(tmp_path):
    class Boom:
        def get_pr(self, n):
            raise RuntimeError("github down")
    t, audit = tracker(tmp_path, Boom(), FakeArgo(SYNCED), FakeKube(snapshot()))
    t._safe_track("inc-8", TARGET, DETAIL)
    assert audit.events("inc-8")[-1]["event"] == "tracker_failed"


def test_timeline_and_resume_inputs_come_from_the_audit_log_only(tmp_path):
    audit = AuditLog(tmp_path)
    seed_audit(audit)
    tgt, detail = rebuild_tracking_inputs(audit.events("inc-1"))
    assert tgt == {"namespace": NS, "name": APP} and detail["number"] == 7 and detail["argocd_app"] == APP
    tl = build_timeline(audit.events("inc-1"))
    assert tl["pr_url"].endswith("/pull/7") and tl["stages"]["pr_open_to_human_merge_s"] is None
    with pytest.raises(ValueError):
        rebuild_tracking_inputs([])


def test_argocd_summary_exposes_autosync_and_prune_flags():
    s = summarize_application({"spec": {"syncPolicy": {"automated": {"prune": False}}},
                               "status": {"sync": {"status": "Synced", "revision": "x"}, "health": {"status": "Healthy"}}})
    assert s["auto_sync"] is True and s["prune"] is False and s["sync_status"] == "Synced"


def test_github_client_reports_merge_state():
    def handler(req):
        assert req.url.path == "/repos/o/r/pulls/7"
        return httpx.Response(200, json={"state": "closed", "merged_at": "2026-11-10T10:05:00Z", "merge_commit_sha": "sha1",
                                         "merged_by": {"login": "riyan-ahmed"}})
    pr = GitHubPR("o/r", "t", transport=httpx.MockTransport(handler)).get_pr(7)
    assert pr["merged"] and pr["merge_commit_sha"] == "sha1" and pr["merged_by"] == "riyan-ahmed"


# ---- end to end through the LangGraph (fake Kubernetes/Prometheus/Loki/GitHub/Argo) ----------------------
def test_graph_opens_a_pr_without_touching_the_cluster_then_tracks_recovery(make_deps, tmp_path, monkeypatch):
    data = copy.deepcopy(Policy.load(ROOT / "policies" / "policy.yaml").data)
    data["environments"]["skillpulse-stg"]["pr_without_preapproval"] = ["propose_config_pr"]
    data["gitops_targets"]["skillpulse-stg"]["backend-sim"] = {
        "path": "k8s/gitops/oom-demo/patches/resources.yaml", "container": "backend", "patch_requests": False, "argocd_app": APP}
    deps, graph, apps = make_deps("oom", mode="approval", dry_run=True, history=False)
    deps.policy = Policy(data)
    deps.settings = Settings(mode="approval", dry_run=True, pr_dry_run=False, audit_dir=deps.settings.audit_dir, github_enabled=True,
                             github_token="t", github_repo="o/r", pr_wait_timeout_s=30, argocd_wait_timeout_s=30,
                             verify_timeout_s=30, tracker_poll_s=1)
    started = {}

    class RecordingTracker:
        def start(self, iid, target, detail):
            started.update(iid=iid, target=target, detail=detail)
    deps.tracker = RecordingTracker()
    import aiops.agent.actions as actions
    real = actions.propose_config_change

    def handler(req):
        p = req.url.path
        if req.method == "GET" and "/git/ref/" in p:
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if req.method == "GET" and "/contents/" in p:
            return contents_response(REAL_FILE.replace("        - name: oom-demo", "        - name: backend"))
        if req.method in ("POST", "PUT") and p.endswith(("/git/refs", "/patches/resources.yaml")):
            return httpx.Response(201, json={})
        if req.method == "POST" and p.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7", "number": 7, "created_at": "2026-11-10T10:00:10Z"})
        return httpx.Response(404)
    monkeypatch.setattr(actions, "propose_config_change", lambda **kw: real(**kw, transport=httpx.MockTransport(handler)))
    from aiops.agent.graph import build_graph
    graph = build_graph(deps)
    st = run_incident(graph, alert())
    assert not st["_pending"], "no approval step is needed to OPEN the PR"
    assert st["policy"]["decision"] == "pr_allowed" and st["approval"]["status"] == "not_required"
    assert st["execution"]["status"] == "executed" and st["execution"]["detail"]["url"].endswith("/pull/7")
    assert st["verification"]["status"] == "tracking" and started["detail"]["number"] == 7
    assert apps.patches == [], "the PR path must never patch the Deployment in the cluster"
    events = [e["event"] for e in deps.audit.events(st["incident_id"])]
    assert "action_executed" in events and "recovery_verification" in events and events[-1] == "incident_closed"


# ---- manifests --------------------------------------------------------------------------------------------
def render(path):
    out = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def test_oom_demo_application_has_automatic_sync_and_no_pruning():
    app = yaml.safe_load((REPO / "gitops/argocd/oom-demo/application.yaml").read_text())
    pol = app["spec"]["syncPolicy"]
    assert pol["automated"]["prune"] is False and pol["automated"].get("selfHeal", False) is False
    assert "finalizers" not in app["metadata"]
    assert "Prune=true" not in str(pol) and "Replace=true" not in pol["syncOptions"] and "Force=true" not in pol["syncOptions"]
    assert app["spec"]["destination"]["namespace"] == NS and app["spec"]["project"] == "oom-demo"
    assert app["spec"]["source"]["path"] == "k8s/gitops/oom-demo" and app["spec"]["source"]["targetRevision"] == "gitops-demo"


def test_oom_demo_project_is_isolated():
    proj = yaml.safe_load((REPO / "gitops/argocd/oom-demo/project.yaml").read_text())["spec"]
    assert proj["clusterResourceWhitelist"] == []
    assert [w["kind"] for w in proj["namespaceResourceWhitelist"]] == ["Deployment"]
    assert [d["namespace"] for d in proj["destinations"]] == [NS]
    assert proj["sourceRepos"] == ["https://github.com/riyan-ahmed/github-actions-kubernetes-masterclass.git"]


def test_existing_applications_and_project_are_unchanged_by_the_demo():
    """The new auto-sync Application lives in its own directory; the manual-sync estate keeps its invariants."""
    apps = {p.stem for p in (REPO / "gitops/argocd/apps").glob("*.yaml")}
    assert apps == {"skillpulse-sandbox", "skillpulse-stg", "skillpulse-prd"}
    for p in (REPO / "gitops/argocd/apps").glob("*.yaml"):
        assert "automated" not in yaml.safe_load(p.read_text())["spec"].get("syncPolicy", {})


@NEEDS_KUBECTL
def test_oom_demo_renders_one_deployment_with_prune_protection():
    docs = render(REPO / "k8s/gitops/oom-demo")
    assert [d["kind"] for d in docs] == ["Deployment"] and docs[0]["metadata"]["namespace"] == NS
    assert docs[0]["metadata"]["annotations"] == {"argocd.argoproj.io/sync-options": "Prune=false,Delete=false"}
    c = docs[0]["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["limits"] == {"memory": "32Mi"} and c["securityContext"]["readOnlyRootFilesystem"] is True


def test_the_pr_target_file_is_a_limits_only_strategic_merge_patch():
    patch = yaml.safe_load((REPO / "k8s/gitops/oom-demo/patches/resources.yaml").read_text())
    c = patch["spec"]["template"]["spec"]["containers"][0]
    assert set(c) == {"name", "resources"} and set(c["resources"]) == {"limits"} and set(c["resources"]["limits"]) == {"memory"}


@NEEDS_KUBECTL
def test_gitops_pr_overlay_enables_pr_creation_but_not_autonomy_or_live_cluster_writes():
    docs = render(REPO / "k8s/aiops/approval-gitops-pr")
    dep = next(d for d in docs if d["kind"] == "Deployment")
    ctr = dep["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in ctr["env"]}
    assert len([e for e in ctr["env"] if e["name"] == "AIOPS_GITHUB_ENABLED"]) == 1
    assert env["AIOPS_GITHUB_ENABLED"]["value"] == "true" and env["AIOPS_GITHUB_BASE_BRANCH"]["value"] == "gitops-demo"
    assert env["AIOPS_MODE"]["value"] == "approval" and env["AIOPS_DRY_RUN"]["value"] == "true"
    assert env["AIOPS_PR_DRY_RUN"]["value"] == "false", "real PRs, while AIOPS_DRY_RUN=true keeps Kubernetes writes dry-run"
    assert env["AIOPS_GITHUB_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "aiops-github", "key": "token"}
    assert "value" not in env["AIOPS_GITHUB_TOKEN"], "the token is never inline"
    assert ctr["image"].endswith(":0.5.2")
    pol = yaml.safe_load(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["policy.yaml"])
    assert pol["global"]["auto_remediation_enabled"] is False
    assert not any(d["kind"] in ("ClusterRole", "ClusterRoleBinding", "Secret") for d in docs)


@NEEDS_KUBECTL
def test_agent_rbac_has_no_write_permission_on_the_gitops_namespace_or_argocd():
    docs = render(REPO / "k8s/aiops/approval-gitops-pr")
    for d in docs:
        if d["kind"] == "Role" and d["metadata"]["namespace"] in (NS, "argocd"):
            for rule in d["rules"]:
                assert not set(rule["verbs"]) & WRITE_VERBS, (d["metadata"], rule)
    argo = [d for d in docs if d["kind"] == "Role" and d["metadata"]["namespace"] == "argocd"]
    assert len(argo) == 1 and argo[0]["rules"] == [{"apiGroups": ["argoproj.io"], "resources": ["applications"],
                                                     "resourceNames": ["oom-demo"], "verbs": ["get"]}]
    # the only write grant anywhere is still the pre-existing staging backend-sim patch
    writers = [(d["metadata"]["namespace"], r["resources"], r.get("resourceNames")) for d in docs if d["kind"] == "Role"
               for r in d["rules"] if set(r["verbs"]) & WRITE_VERBS]
    assert writers == [("skillpulse-stg", ["deployments"], ["backend-sim"])]


def test_demo_scripts_never_use_kubectl_to_change_the_workload():
    lines = (REPO / "scripts" / "gitops-oom-demo.sh").read_text().splitlines()
    text = "\n".join(l for l in lines if not l.lstrip().startswith("#"))      # comments may name the forbidden commands
    assert "kubectl" in text and "git_edit" in text
    for forbidden in ("rollout undo", "kubectl patch", "kubectl edit", "kubectl set ", "kubectl scale", "kubectl delete",
                      "kubectl apply", "kubectl replace", "argocd app sync", "kubectl annotate"):
        assert forbidden not in text, forbidden


# ---- minimal-diff editing ---------------------------------------------------------------------------------
def test_edit_changes_exactly_one_line_and_keeps_comments():
    text = "# keep me\n" + REAL_FILE
    new = edit_memory_limit(text, "oom-demo", "128Mi")
    old_l, new_l = text.splitlines(), new.splitlines()
    assert len(old_l) == len(new_l) and [i for i, (a, b) in enumerate(zip(old_l, new_l)) if a != b] == [
        next(i for i, l in enumerate(old_l) if "memory:" in l)]
    assert new.startswith("# keep me\n") and "128Mi" in new and "32Mi" not in new


@pytest.mark.parametrize("bad", [
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: x}\nspec: {template: {spec: {containers: []}}}\n",
    "spec:\n  template:\n    spec:\n      containers:\n        - name: other\n          resources:\n            limits:\n              memory: 32Mi\n",
    "spec:\n  template:\n    spec:\n      containers:\n        - name: oom-demo\n          resources:\n            limits: {memory: 32Mi}\n",
])
def test_edit_fails_closed_on_unexpected_shapes(bad):
    with pytest.raises(ValueError):
        edit_memory_limit(bad, "oom-demo", "64Mi")


def test_pr_is_refused_if_the_target_file_does_not_exist_on_the_base_branch(policy, tmp_path):
    def handler(req):
        p = req.url.path
        if req.method == "GET" and "/git/ref/" in p:
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if req.method == "POST" and p.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if req.method == "GET" and "/contents/" in p:
            return httpx.Response(404, json={})
        raise AssertionError(f"unexpected call {req.method} {p}: nothing may be written")
    s, tr = github(tmp_path, handler)
    with pytest.raises(ValueError):
        propose_config_change(settings=s, policy=policy, incident_id="inc-x", namespace=NS, workload=APP, params=PARAMS,
                              rationale="", dry_run=False, transport=tr)
