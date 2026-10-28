"""Offline evaluation of the agent's RBAC and the safety posture of its deployment manifests."""
import shutil
import subprocess

import pytest
import yaml

from aiops.tests.conftest import ROOT

DEPLOY = ROOT / "deploy"
KUSTOMIZATION = yaml.safe_load((ROOT / "kustomization.yaml").read_text())
APPLIED_FILES = [ROOT / r for r in KUSTOMIZATION["resources"]]


def load(paths):
    return [d for p in paths for d in yaml.safe_load_all(p.read_text()) if d]


APPLIED = load(APPLIED_FILES)
REMEDIATOR = load([ROOT / "components" / "remediator-rbac" / "rbac-remediator-backend-sim.yaml"])
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*", "impersonate", "escalate", "bind"}


def allowed(docs, namespace, verb, group, resource, name=None):
    for r in (d for d in docs if d["kind"] == "Role" and d["metadata"]["namespace"] == namespace):
        for rule in r["rules"]:
            if verb in rule["verbs"] and group in rule["apiGroups"] and resource in rule["resources"]:
                if "resourceNames" in rule and name not in rule["resourceNames"]:
                    continue
                return True
    return False


# ---- what is applied now: strictly read-only -------------------------------------------------
def test_applied_rbac_has_no_write_verbs_anywhere():
    for d in APPLIED:
        if d["kind"] in ("Role", "ClusterRole"):
            for rule in d["rules"]:
                assert not set(rule["verbs"]) & WRITE_VERBS, (d["metadata"]["name"], rule)


def test_applied_rbac_is_namespaced_and_limited_to_stg_and_prd():
    assert not [d for d in APPLIED if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    roles = [d for d in APPLIED if d["kind"] == "Role"]
    assert sorted(r["metadata"]["namespace"] for r in roles) == ["skillpulse-prd", "skillpulse-stg"]


@pytest.mark.parametrize("ns", ["skillpulse-stg", "skillpulse-prd"])
@pytest.mark.parametrize("verb", ["create", "update", "patch", "delete", "deletecollection"])
@pytest.mark.parametrize("res", [("", "pods"), ("apps", "deployments"), ("apps", "statefulsets"), ("", "persistentvolumeclaims")])
@pytest.mark.parametrize("name", ["backend-sim", "backend", "mysql"])
def test_nothing_can_be_modified_now_including_backend_sim(ns, verb, res, name):
    assert not allowed(APPLIED, ns, verb, *res, name=name)


@pytest.mark.parametrize("ns", ["skillpulse-stg", "skillpulse-prd"])
@pytest.mark.parametrize("res", [("", "secrets"), ("", "configmaps"), ("", "pods/exec"), ("", "persistentvolumeclaims")])
def test_no_secrets_configmaps_exec_or_pvcs(ns, res):
    for verb in ("get", "list", "watch", "create"):
        assert not allowed(APPLIED, ns, verb, *res)


def test_evidence_reads_available():
    for ns in ("skillpulse-stg", "skillpulse-prd"):
        for res in (("", "pods"), ("", "events"), ("apps", "replicasets"), ("apps", "deployments")):
            assert allowed(APPLIED, ns, "list", *res)
        assert allowed(APPLIED, ns, "get", "", "pods/log")


def test_dev_and_other_namespaces_not_granted():
    for ns in ("skillpulse-dev", "skillpulse", "kube-system", "monitoring", "aiops"):
        assert not allowed(APPLIED, ns, "get", "", "pods")


def test_bindings_target_only_the_agent_serviceaccount():
    for b in (d for d in APPLIED + REMEDIATOR if d["kind"] == "RoleBinding"):
        assert b["subjects"] == [{"kind": "ServiceAccount", "name": "aiops-agent", "namespace": "aiops"}]


# ---- the reserved remediation grant: never applied by default, and as narrow as possible ---------
def test_remediator_grant_is_not_part_of_the_applied_set():
    assert "rbac-remediator-backend-sim.yaml" not in " ".join(KUSTOMIZATION["resources"])
    assert not any(d["metadata"]["name"] == "aiops-remediator-backend-sim" for d in APPLIED)


def test_reserved_remediator_can_only_patch_backend_sim():
    assert allowed(REMEDIATOR, "skillpulse-stg", "patch", "apps", "deployments", "backend-sim")
    for name in ("backend", "frontend", "mysql"):
        assert not allowed(REMEDIATOR, "skillpulse-stg", "patch", "apps", "deployments", name)
    assert not allowed(REMEDIATOR, "skillpulse-prd", "patch", "apps", "deployments", "backend-sim")
    for verb in ("create", "update", "delete"):
        assert not allowed(REMEDIATOR, "skillpulse-stg", verb, "apps", "deployments", "backend-sim")


# ---- deployment manifest safety posture ----------------------------------------------------------
DEPLOYMENT = next(d for d in APPLIED if d["kind"] == "Deployment")
POD = DEPLOYMENT["spec"]["template"]["spec"]
CONTAINER = POD["containers"][0]
ENV = {e["name"]: e for e in CONTAINER["env"]}


def test_deployed_in_safe_default_mode():
    assert ENV["AIOPS_MODE"]["value"] == "diagnose"
    assert ENV["AIOPS_DRY_RUN"]["value"] == "true"
    assert ENV["AIOPS_LLM_PROVIDER"]["value"] == "none"
    assert ENV["AIOPS_GITHUB_ENABLED"]["value"] == "false"


def test_tokens_only_from_required_secret_refs():
    for name, secret in (("AIOPS_WEBHOOK_TOKEN", "aiops-webhook"), ("AIOPS_APPROVAL_TOKEN", "aiops-approval")):
        ref = ENV[name]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == secret and ref["key"] == "token"
        assert not ref.get("optional"), "a missing Secret must stop the pod, not start it unauthenticated"
        assert "value" not in ENV[name]


def test_container_is_hardened():
    sc, psc = CONTAINER["securityContext"], POD["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert psc["runAsNonRoot"] is True and psc["runAsUser"] != 0
    assert psc["seccompProfile"]["type"] == "RuntimeDefault"
    assert not POD.get("hostNetwork") and not POD.get("hostPID")
    assert CONTAINER["resources"]["limits"]["memory"]


def test_image_is_pinned_not_latest():
    assert CONTAINER["image"].split(":")[-1] not in ("latest", CONTAINER["image"])


def test_kustomize_renders_only_expected_kinds():
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl not available (for example inside a minimal container)")
    out = subprocess.run(["kubectl", "kustomize", str(ROOT)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    kinds = sorted(d["kind"] for d in yaml.safe_load_all(out.stdout) if d)
    assert kinds == sorted(["ServiceAccount", "Role", "Role", "RoleBinding", "RoleBinding", "ConfigMap", "Service",
                            "PersistentVolumeClaim", "Deployment"])
    assert "kind: Secret" not in out.stdout and "ClusterRole" not in out.stdout
