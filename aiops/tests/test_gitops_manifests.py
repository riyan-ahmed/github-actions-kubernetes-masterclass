"""Argo CD / GitOps manifests: manual sync, no pruning, no adoption surprises, no plaintext secrets."""
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
ARGO = REPO / "gitops" / "argocd"
GITOPS = REPO / "k8s" / "gitops"
NEEDS_KUBECTL = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not available")

PROJECT = yaml.safe_load((ARGO / "project.yaml").read_text())
APPS = {p.stem: yaml.safe_load(p.read_text()) for p in sorted((ARGO / "apps").glob("*.yaml"))}


# ---- AppProject -------------------------------------------------------------------------------
def test_project_only_allows_the_sandbox_namespace():
    """Initial sandbox test: stg and prd destinations are added only after a separate review."""
    ns = {d["namespace"] for d in PROJECT["spec"]["destinations"]}
    assert ns == {"skillpulse-gitops"}
    assert not ns & {"kube-system", "monitoring", "aiops", "argocd", "default", "skillpulse-dev", "skillpulse",
                     "skillpulse-stg", "skillpulse-prd", "*"}
    assert all(d["server"] == "https://kubernetes.default.svc" for d in PROJECT["spec"]["destinations"])


def test_project_forbids_cluster_resources_and_plaintext_secrets():
    assert PROJECT["spec"]["clusterResourceWhitelist"] == []
    kinds = {w["kind"] for w in PROJECT["spec"]["namespaceResourceWhitelist"]}
    assert "Secret" not in kinds and "*" not in kinds
    assert "SealedSecret" in kinds
    assert not any(w.get("kind") == "*" or w.get("group") == "*" for w in PROJECT["spec"]["namespaceResourceWhitelist"])


def test_project_source_is_a_single_pinned_repo():
    assert PROJECT["spec"]["sourceRepos"] == ["https://github.com/riyan-ahmed/github-actions-kubernetes-masterclass.git"]


# ---- Applications -----------------------------------------------------------------------------
def test_there_are_exactly_sandbox_stg_prd_applications():
    assert set(APPS) == {"skillpulse-sandbox", "skillpulse-stg", "skillpulse-prd"}


@pytest.mark.parametrize("name", sorted(APPS))
def test_application_is_manual_sync_without_prune_or_finalizer(name):
    app = APPS[name]
    policy = app["spec"].get("syncPolicy", {})
    assert "automated" not in policy, "auto-sync / self-heal / auto-prune must never be enabled"
    assert "prune" not in str(policy).lower() or "PruneLast" in str(policy)
    assert "finalizers" not in app["metadata"], "a finalizer would cascade-delete workloads when the Application is removed"
    assert "CreateNamespace=false" in policy["syncOptions"]
    assert "Replace=true" not in policy["syncOptions"] and "Force=true" not in policy["syncOptions"]


@pytest.mark.parametrize("name", sorted(APPS))
def test_application_targets_an_allowed_destination_repo_and_existing_path(name):
    app = APPS[name]
    assert app["spec"]["project"] == "skillpulse"
    assert app["spec"]["source"]["repoURL"] in PROJECT["spec"]["sourceRepos"]
    allowed = {d["namespace"] for d in PROJECT["spec"]["destinations"]}
    if name == "skillpulse-sandbox":
        assert app["spec"]["destination"]["namespace"] in allowed
    else:   # stg/prd Applications are written but must stay unusable until the project is widened after review
        assert app["spec"]["destination"]["namespace"] not in allowed
    assert app["spec"]["source"]["targetRevision"] not in ("", "*", None)
    assert (REPO / app["spec"]["source"]["path"] / "kustomization.yaml").exists()


def test_sandbox_application_tracks_the_feature_branch_not_main():
    assert APPS["skillpulse-sandbox"]["spec"]["source"]["targetRevision"] == "feature/aiops-self-healing"


def test_adopting_applications_carry_a_migration_warning_and_the_sandbox_does_not_adopt():
    for n in ("skillpulse-stg", "skillpulse-prd"):
        assert "ADOPTS EXISTING" in APPS[n]["metadata"]["annotations"]["aiops.skillpulse/note"]
    assert APPS["skillpulse-sandbox"]["spec"]["destination"]["namespace"] == "skillpulse-gitops"
    assert "NON-ADOPTING" in APPS["skillpulse-sandbox"]["metadata"]["annotations"]["aiops.skillpulse/note"]


def test_argocd_install_values_are_minimal_and_safe():
    v = yaml.safe_load((ARGO / "install" / "argocd-values.yaml").read_text())
    assert v["dex"]["enabled"] is False and v["notifications"]["enabled"] is False
    assert v["applicationSet"]["replicas"] == 0
    assert v["configs"]["cm"]["exec.enabled"] is False
    assert v["configs"]["cm"]["application.resourceTrackingMethod"] == "annotation"


# ---- rendered overlays ------------------------------------------------------------------------
def render(path: Path) -> list[dict]:
    out = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


@NEEDS_KUBECTL
@pytest.mark.parametrize("env,ns", [("stg", "skillpulse-stg"), ("prd", "skillpulse-prd"), ("sandbox", "skillpulse-gitops")])
def test_overlay_renders_safe_objects(env, ns):
    docs = render(GITOPS / env)
    assert sorted(d["kind"] for d in docs) == ["ConfigMap", "Deployment", "Deployment", "Service", "Service", "Service", "StatefulSet"]
    assert {d["metadata"]["namespace"] for d in docs} == {ns}
    for d in docs:
        assert d["metadata"]["annotations"] == {"argocd.argoproj.io/sync-options": "Prune=false,Delete=false"}, d["metadata"]["name"]
        if d["kind"] in ("Deployment", "StatefulSet"):
            assert "annotations" not in d["spec"]["template"]["metadata"], "pod-template change would restart workloads on first sync"
    assert not any(d["kind"] == "Secret" for d in docs)


@NEEDS_KUBECTL
def test_prd_keeps_two_backend_replicas_and_the_resource_patch_is_a_noop_baseline():
    prd = {(d["kind"], d["metadata"]["name"]): d for d in render(GITOPS / "prd")}
    assert prd[("Deployment", "backend")]["spec"]["replicas"] == 2
    base = {(d["kind"], d["metadata"]["name"]): d for d in render(REPO / "k8s" / "base")}
    for env in ("stg", "prd", "sandbox"):
        r = {(d["kind"], d["metadata"]["name"]): d for d in render(GITOPS / env)}
        got = r[("Deployment", "backend")]["spec"]["template"]["spec"]["containers"][0]
        want = base[("Deployment", "backend")]["spec"]["template"]["spec"]["containers"][0]
        assert got["resources"] == want["resources"] and got["image"] == want["image"]
        assert r[("StatefulSet", "mysql")]["spec"] == base[("StatefulSet", "mysql")]["spec"], "mysql must not change"


@NEEDS_KUBECTL
@pytest.mark.parametrize("env", ["stg", "prd"])
def test_parity_with_the_existing_overlays_when_their_local_secret_files_exist(env):
    if not (REPO / "k8s" / "overlays" / env / "db-secret.env").exists():
        pytest.skip("git-ignored db-secret.env not present (CI)")
    old = {(d["kind"], d["metadata"]["name"]): d for d in render(REPO / "k8s" / "overlays" / env)}
    new = {(d["kind"], d["metadata"]["name"]): d for d in render(GITOPS / env)}
    assert set(old) - set(new) == {("Secret", "skillpulse-db")} and not set(new) - set(old)
    for k, d in new.items():
        d = {**d, "metadata": {x: y for x, y in d["metadata"].items() if x != "annotations"}}
        assert d == old[k], f"{k} differs from the live overlay"


# ---- no plaintext secrets ---------------------------------------------------------------------
def test_no_plaintext_secret_manifests_anywhere_in_the_gitops_tree():
    for p in list(GITOPS.rglob("*.yaml")) + list(ARGO.rglob("*.yaml")):
        text = p.read_text()
        for doc in yaml.safe_load_all(text):
            if not doc:
                continue
            assert doc.get("kind") != "Secret", f"plaintext Secret in {p}"
            if doc.get("kind") == "SealedSecret":
                assert "encryptedData" in doc["spec"] and "data" not in doc and "stringData" not in doc["spec"]
        assert "stringData" not in text, p


def test_env_files_stay_git_ignored():
    ig = (REPO / ".gitignore").read_text()
    assert "k8s/overlays/*/db-secret.env" in ig and "aiops/audit/*" in ig and "aiops/.venv/" in ig


# ---- scripts/seal-db-secret.sh ----------------------------------------------------------------
SCRIPT = REPO / "scripts" / "seal-db-secret.sh"


def run_script(tmp_path, kubeseal_body, *args):
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl not available")
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, work / "scripts" / "seal-db-secret.sh")
    envf = work / "k8s" / "overlays" / "stg" / "db-secret.env"
    envf.parent.mkdir(parents=True)
    envf.write_text("MYSQL_USER=app\nMYSQL_PASSWORD=Sup3rS3cretPw!\nMYSQL_DATABASE=skillpulse\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ks = bindir / "kubeseal"
    ks.write_text("#!/usr/bin/env bash\n" + kubeseal_body)
    ks.chmod(ks.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "scripts/seal-db-secret.sh", *args], cwd=work, env=env, capture_output=True, text=True)
    return r, work


FAKE_SEAL = textwrap.dedent("""\
    cat >/dev/null   # consume the plaintext Secret from stdin
    cat <<'YAML'
    apiVersion: bitnami.com/v1alpha1
    kind: SealedSecret
    metadata:
      name: skillpulse-db
      namespace: skillpulse-stg
    spec:
      encryptedData:
        MYSQL_USER: AgBy3i4OJSWK+fakeciphertext
        MYSQL_PASSWORD: AgCX9+fakeciphertext
      template:
        metadata:
          name: skillpulse-db
    YAML
    """)


def test_seal_script_writes_only_encrypted_output_and_never_prints_the_secret(tmp_path):
    r, work = run_script(tmp_path, FAKE_SEAL, "stg")
    assert r.returncode == 0, r.stderr
    out = work / "k8s" / "gitops" / "secrets" / "stg"
    sealed = (out / "sealed-secret.yaml").read_text()
    assert "kind: SealedSecret" in sealed and "encryptedData" in sealed
    assert "Sup3rS3cretPw" not in sealed + r.stdout + r.stderr
    assert (out / "kustomization.yaml").exists()
    assert (out / "sealed-secret.yaml").stat().st_mode & 0o077 == 0, "output must be owner-only (umask 077)"
    assert not list(out.glob(".sealed.*")), "temporary file must be cleaned up"


def test_seal_script_refuses_when_kubeseal_returns_plaintext(tmp_path):
    r, work = run_script(tmp_path, "cat   # a broken kubeseal that echoes the plaintext Secret back\n", "stg")
    assert r.returncode != 0
    assert not (work / "k8s" / "gitops" / "secrets" / "stg" / "sealed-secret.yaml").exists()
    assert "Sup3rS3cretPw" not in r.stdout + r.stderr


def test_seal_script_adopt_flag_adds_the_managed_annotation(tmp_path):
    r, work = run_script(tmp_path, FAKE_SEAL, "stg", "--adopt-existing")
    assert r.returncode == 0, r.stderr
    text = (work / "k8s" / "gitops" / "secrets" / "stg" / "sealed-secret.yaml").read_text()
    assert 'sealedsecrets.bitnami.com/managed: "true"' in text
    assert yaml.safe_load(text)["metadata"]["annotations"]["sealedsecrets.bitnami.com/managed"] == "true"


def test_seal_script_rejects_unknown_environment_and_missing_file(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    r, _ = run_script(tmp_path / "a", FAKE_SEAL, "dev")
    assert r.returncode == 2
    r, _ = run_script(tmp_path / "b", FAKE_SEAL, "prd")
    assert r.returncode == 1 and "missing" in r.stderr


# ---- Sealed Secrets install values (sandbox-scoped, no cluster-wide Secret access) -----------------
SEALED = yaml.safe_load((ARGO / "install" / "sealed-secrets-values.yaml").read_text())


def test_sealed_secrets_has_no_cluster_role_and_is_scoped_to_the_sandbox():
    assert SEALED["rbac"]["clusterRole"] is False and SEALED["rbac"]["namespacedRoles"] is True
    assert set(SEALED["additionalNamespaces"]) == {"sealed-secrets", "skillpulse-gitops"}
    assert "--all-namespaces=false" in SEALED["args"]
    assert "--additional-namespaces=skillpulse-gitops,sealed-secrets" in SEALED["args"]


def test_seal_script_targets_the_scoped_controller_namespace():
    assert "SEALED_SECRETS_NAMESPACE:-sealed-secrets" in (REPO / "scripts" / "seal-db-secret.sh").read_text()


def test_install_docs_create_the_sandbox_namespace_before_any_helm_install():
    """A scoped Sealed Secrets chart creates a Role in skillpulse-gitops: the namespace must exist first (found in practice)."""
    text = (REPO / "docs" / "aiops-setup.md").read_text()
    install = text.split("### 4.2 Install", 1)[1].split("### 4.3", 1)[0]
    assert install.index("kubectl create namespace skillpulse-gitops") < install.index("helm install sealed-secrets")
    assert install.index("helm install sealed-secrets") < install.index("helm install argocd")
    assert "validate_install_order.py" in install
