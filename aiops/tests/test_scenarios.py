"""Failure scenarios: isolated to backend-sim, exactly one operative difference, guarded scripts."""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

SC = Path(__file__).resolve().parents[1] / "scenarios"
FAULTS = {"crashloop": "crashloop/backend-sim-v2-invalid-port.yaml", "oom": "oom/backend-sim-v2-oom.yaml",
          "readiness": "readiness/backend-sim-v2-bad-probe.yaml"}


def load(rel):
    return yaml.safe_load((SC / rel).read_text())


def leaves(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, path + (i,))
    else:
        yield path, obj


def diff_paths(a, b):
    la, lb = dict(leaves(a)), dict(leaves(b))
    return {p for p in set(la) | set(lb) if la.get(p) != lb.get(p)}


BASE = load("common/backend-sim-v1.yaml")


@pytest.mark.parametrize("name", [BASE] + [load(p) for p in FAULTS.values()], ids=["v1"] + list(FAULTS))
def test_every_manifest_is_isolated_to_backend_sim_in_staging(name):
    assert name["kind"] == "Deployment" and name["metadata"]["name"] == "backend-sim"
    assert name["metadata"]["namespace"] == "skillpulse-stg"
    assert name["spec"]["replicas"] == 1
    assert name["spec"]["selector"]["matchLabels"] == {"app": "backend-sim"}
    assert name["spec"]["template"]["metadata"]["labels"]["app"] == "backend-sim", "must not match the real Services' selectors"
    assert "argocd" not in str(name).lower()


@pytest.mark.parametrize("name", [BASE] + [load(p) for p in FAULTS.values()], ids=["v1"] + list(FAULTS))
def test_manifests_are_hardened_and_use_secrets_only_by_reference(name):
    pod = name["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    secret_envs = [e for e in c["env"] if e["name"] in ("DB_USER", "DB_PASSWORD", "DB_NAME")]
    assert all("valueFrom" in e and "value" not in e for e in secret_envs)
    assert not any("PASSWORD" in e["name"] and "value" in e for e in c["env"])


def test_there_is_no_service_for_backend_sim_so_it_never_receives_traffic():
    for p in SC.rglob("*.yaml"):
        for d in yaml.safe_load_all(p.read_text()):
            assert d and d["kind"] != "Service", p


@pytest.mark.parametrize("scenario,expected", [
    ("crashloop", {("spec", "template", "spec", "containers", 0, "env", 2, "value")}),
    ("oom", {("spec", "template", "spec", "containers", 0, "resources", "requests", "memory"),
             ("spec", "template", "spec", "containers", 0, "resources", "limits", "memory")}),
    ("readiness", {("spec", "template", "spec", "containers", 0, "readinessProbe", "httpGet", "path")}),
])
def test_each_fault_differs_from_the_baseline_by_exactly_the_intended_fields(scenario, expected):
    changed = diff_paths(BASE, load(FAULTS[scenario])) - {("metadata", "annotations", "kubernetes.io/change-cause")}
    assert changed == expected


def test_the_oom_limit_is_the_empirically_verified_one():
    c = load(FAULTS["oom"])["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["limits"]["memory"] == "6Mi"        # 8Mi does not OOM; 3Mi cannot start (measured)
    assert c["resources"]["requests"]["memory"] == "6Mi"      # request must not exceed the limit


def test_baseline_values_the_scripts_rely_on():
    c = BASE["spec"]["template"]["spec"]["containers"][0]
    assert {e["name"]: e.get("value") for e in c["env"]}["PORT"] == "8080"
    assert c["resources"]["limits"]["memory"] == "128Mi" and c["readinessProbe"]["httpGet"]["path"] == "/health"


# ---- scripts ------------------------------------------------------------------------------------
SCRIPT = SC / "scenario.sh"


def shim(tmp_path, body):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    k = b / "kubectl"
    k.write_text("#!/usr/bin/env bash\n" + body)
    k.chmod(k.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, "PATH": f"{b}:{os.environ['PATH']}"}


def run(tmp_path, body, *args):
    r = subprocess.run(["bash", str(SCRIPT), *args], env=shim(tmp_path, body), capture_output=True, text=True, timeout=30)
    return r


def test_script_refuses_to_run_in_the_wrong_cluster_context(tmp_path):
    r = run(tmp_path, 'if [ "$1" = config ]; then echo some-prod-cluster; exit 0; fi; echo "KUBECTL CALLED: $*" >> ' + str(tmp_path / "calls"), "oom", "inject")
    assert r.returncode == 1 and "refusing" in r.stderr
    assert not (tmp_path / "calls").exists(), "no cluster call may happen in the wrong context"


UNHEALTHY = r'''
if [ "$1" = config ]; then echo kind-skillpulse; exit 0; fi
echo "$*" >> %s
case "$*" in
  *"get deploy backend-sim -o jsonpath"*) echo "0/1"; exit 0 ;;
esac
exit 0
'''


def test_inject_refuses_unless_the_baseline_is_healthy_and_never_applies(tmp_path):
    calls = tmp_path / "calls"
    r = run(tmp_path, UNHEALTHY % calls, "oom", "inject")
    assert r.returncode == 1 and "not a healthy baseline" in r.stderr
    assert not any(" apply " in f" {l} " for l in calls.read_text().splitlines()), calls.read_text()


@pytest.mark.parametrize("args", [("nonsense", "status"), ("oom", "delete-everything"), ("oom",)])
def test_unknown_scenarios_and_actions_are_rejected(tmp_path, args):
    r = run(tmp_path, 'if [ "$1" = config ]; then echo kind-skillpulse; exit 0; fi\nexit 0\n', *args)
    assert r.returncode != 0


def test_scripts_can_only_name_the_staging_namespace_and_backend_sim():
    text = SCRIPT.read_text() + (SC / "tools" / "observe.py").read_text()
    assert "NS=skillpulse-stg" in text and "DEP=backend-sim" in text and 'NS = "skillpulse-stg"' in text
    for forbidden in ("skillpulse-prd", "-n skillpulse ", "mysql", "delete ", "scale ", "--force", "exec -it"):
        if forbidden == "delete ":
            assert not re.search(r"kubectl[^\n]*\bdelete\b", text)
        else:
            assert forbidden not in text.replace("rollout undo", ""), forbidden
    assert "POST" not in (SC / "tools" / "observe.py").read_text().replace("never POSTs", "").replace("never POST", "").replace("manual POST", ""), \
        "the observer must be read-only after injection"
