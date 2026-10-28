"""Approval-gated remediation: scope, defaults, overlays, approval script and report."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from aiops.agent.graph import resume_incident, run_incident
from aiops.tests.conftest import alert

REPO = Path(__file__).resolve().parents[2]
NEEDS_KUBECTL = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not available")
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*", "escalate", "bind", "impersonate"}


def render(path):
    out = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def env_of(docs):
    dep = next(d for d in docs if d["kind"] == "Deployment")
    return {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}


def writes(docs):
    """(namespace, resource, resourceNames, verbs) for every rule that grants a write verb."""
    out = []
    for d in docs:
        if d["kind"] == "Role":
            for r in d["rules"]:
                if set(r["verbs"]) & WRITE_VERBS:
                    out.append((d["metadata"]["namespace"], tuple(r["resources"]), tuple(r.get("resourceNames", [])), tuple(r["verbs"])))
    return out


# ---- default deployment stays read-only --------------------------------------------------------
@NEEDS_KUBECTL
def test_default_deployment_has_no_write_rule_and_stays_diagnose():
    docs = render(REPO / "aiops")
    assert writes(docs) == []
    assert env_of(docs)["AIOPS_MODE"] == "diagnose" and env_of(docs)["AIOPS_DRY_RUN"] == "true"


# ---- approval overlays --------------------------------------------------------------------------
@NEEDS_KUBECTL
@pytest.mark.parametrize("overlay,dry", [("approval-dryrun", "true"), ("approval-live", "false")])
def test_approval_overlays_enable_approval_not_autonomy(overlay, dry):
    docs = render(REPO / "k8s" / "aiops" / overlay)
    e = env_of(docs)
    assert e["AIOPS_MODE"] == "approval" and e["AIOPS_DRY_RUN"] == dry
    assert e["AIOPS_MODE"] != "auto"
    assert e["AIOPS_LLM_PROVIDER"] == "none" and e["AIOPS_GITHUB_ENABLED"] == "false"
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    policy = yaml.safe_load(cm["data"]["policy.yaml"])
    assert policy["global"]["auto_remediation_enabled"] is False, "autonomous remediation must stay disabled"
    assert not any(d["kind"] in ("ClusterRole", "ClusterRoleBinding", "Secret") for d in docs)


@NEEDS_KUBECTL
@pytest.mark.parametrize("overlay", ["approval-dryrun", "approval-live"])
def test_the_only_write_permission_is_get_patch_on_backend_sim_in_staging(overlay):
    w = writes(render(REPO / "k8s" / "aiops" / overlay))
    assert w == [("skillpulse-stg", ("deployments",), ("backend-sim",), ("get", "patch"))], w


@NEEDS_KUBECTL
def test_overlays_differ_from_default_only_by_the_remediator_role_and_two_env_vars():
    base = {(d["kind"], d["metadata"]["name"], d["metadata"].get("namespace")): d for d in render(REPO / "aiops")}
    live = {(d["kind"], d["metadata"]["name"], d["metadata"].get("namespace")): d for d in render(REPO / "k8s" / "aiops" / "approval-live")}
    extra = set(live) - set(base)
    assert extra == {("Role", "aiops-remediator-backend-sim", "skillpulse-stg"), ("RoleBinding", "aiops-remediator-backend-sim", "skillpulse-stg")}
    assert set(base) - set(live) == set()
    for k, d in base.items():
        if k[0] != "Deployment":
            assert live[k] == d, k
    b = base[("Deployment", "aiops-agent", "aiops")]["spec"]["template"]["spec"]["containers"][0]
    l = live[("Deployment", "aiops-agent", "aiops")]["spec"]["template"]["spec"]["containers"][0]
    assert {x["name"] for x in b["env"] if x not in l["env"]} == {"AIOPS_MODE", "AIOPS_DRY_RUN"}
    assert b["image"] == l["image"] and b["securityContext"] == l["securityContext"]


# ---- scripts/aiops-decide.sh --------------------------------------------------------------------
SCRIPT = REPO / "scripts" / "aiops-decide.sh"
IID = "inc-20261028-130900-ffc817"
APPROVAL_TOKEN, WEBHOOK_TOKEN = "approval-token-SECRET-1111", "webhook-token-SECRET-2222"


def make_shims(tmp_path):
    import base64
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "calls.log"
    (b / "kubectl").write_text(f"""#!/usr/bin/env bash
if [[ "$*" == *"port-forward"* ]]; then sleep 30; exit 0; fi
if [[ "$*" == *"aiops-approval"* ]]; then printf %s '{base64.b64encode(APPROVAL_TOKEN.encode()).decode()}'; exit 0; fi
if [[ "$*" == *"aiops-webhook"* ]]; then printf %s '{base64.b64encode(WEBHOOK_TOKEN.encode()).decode()}'; exit 0; fi
exit 1
""")
    (b / "curl").write_text(f"""#!/usr/bin/env bash
echo "ARGV: $*" >> {log}
cfg=""; for a in "$@"; do if [ "$a" = "-K" ]; then read_cfg=1; fi; done
if [[ "$*" == *"-K"* ]]; then cat /dev/fd/* 2>/dev/null | head -c 300 >> {log}.cfg; fi
case "$*" in
  *healthz*) echo '{{"status":"ok"}}' ;;
  *decision*) echo '{{"status":"closed","outcome":"dry-run only: no change made"}}'; echo 'HTTP 200' ;;
  *report*) echo '{{"report":"# Incident"}}' ;;
  *incidents*) echo '["{IID}"]' ;;
esac
""")
    for f in ("kubectl", "curl"):
        (b / f).chmod((b / f).stat().st_mode | stat.S_IEXEC)
    return b, log


def run_decide(tmp_path, *args):
    b, log = make_shims(tmp_path)
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "AIOPS_LOCAL_PORT": "18999"}
    r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
    return r, log


def test_approve_sends_decision_and_never_exposes_the_token(tmp_path):
    r, log = run_decide(tmp_path, "approve", IID, "--approver", "alice", "--confirm", IID)
    assert r.returncode == 0, r.stderr
    argv = log.read_text()
    assert f"/incidents/{IID}/decision" in argv and '"decision": "approve"' in argv and '"approver": "alice"' in argv
    assert APPROVAL_TOKEN not in argv + r.stdout + r.stderr, "token must not appear in argv or output"
    assert WEBHOOK_TOKEN not in argv + r.stdout + r.stderr
    assert "dry-run only" in r.stdout


def test_decision_requires_approver_and_exact_confirmation(tmp_path):
    for args in (["approve", IID, "--confirm", IID],                                   # no approver
                 ["approve", IID, "--approver", "alice"],                              # no confirm
                 ["approve", IID, "--approver", "alice", "--confirm", "inc-wrong"],    # wrong confirm
                 ["reject", IID, "--approver", "alice", "--confirm", "x"]):
        (tmp_path / "t").mkdir(exist_ok=True)
        r, log = run_decide(tmp_path / "t", *args)
        assert r.returncode == 2
        assert not log.exists() or "decision" not in log.read_text(), "nothing may be sent on a refused decision"
        shutil.rmtree(tmp_path / "t")


def test_malformed_incident_ids_are_rejected_before_any_network_call(tmp_path):
    r, log = run_decide(tmp_path, "report", "../../etc/passwd")
    assert r.returncode == 2 and not log.exists()


def test_list_and_report_use_the_read_token_not_the_approval_token(tmp_path):
    r, log = run_decide(tmp_path, "list")
    assert r.returncode == 0 and IID in r.stdout
    assert APPROVAL_TOKEN not in log.read_text() + r.stdout


# ---- report shows how to approve ----------------------------------------------------------------
@pytest.mark.parametrize("dry,phrase", [(True, "server-side dry run (nothing will change)"), (False, "for real")])
def test_pending_report_tells_the_human_exactly_what_will_happen_and_how_to_decide(make_deps, dry, phrase):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=dry, prom_crash=True)
    st = run_incident(graph, alert())
    md = open(st["report_path"]).read()
    assert "## Approval required" in md and phrase in md
    assert f"scripts/aiops-decide.sh approve {st['incident_id']} --approver <your-name> --confirm {st['incident_id']}" in md
    assert "skillpulse-stg/backend-sim" in md
    assert apps.patches == []


def test_report_has_no_approval_section_in_diagnose_mode(make_deps):
    deps, graph, _ = make_deps("crash", mode="diagnose", prom_crash=True)
    assert "## Approval required" not in open(run_incident(graph, alert())["report_path"]).read()


def test_approved_decision_is_recorded_with_approver_action_and_verification(make_deps):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=False, prom_crash=True)
    st = run_incident(graph, alert())
    final = resume_incident(graph, st["incident_id"], "approve", "alice")
    events = {e["event"]: e for e in deps.audit.events(st["incident_id"])}
    assert events["approval_approved"]["actor"] == "alice"
    ex = events["action_executed"]["data"]
    assert ex["workload"] == "backend-sim" and ex["namespace"] == "skillpulse-stg" and ex["dry_run"] is False
    assert ex["target_replicaset_ready_history"]["was_ready"] is True, "rollback target must be proven healthy first"
    assert ex["from_revision"] == 2 and ex["to_revision"] == 1
    assert events["recovery_verification"]["data"]["status"] == "recovered"
    assert "dry_run" not in final["policy"] or final["policy"]["dry_run"] is False
    assert "Approval status:** approved by alice" in open(final["report_path"]).read()
