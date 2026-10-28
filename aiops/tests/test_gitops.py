import base64
import json

import httpx
import yaml

from aiops.agent.config import Settings
from aiops.agent.gitops import build_resources_patch, propose_config_change

PARAMS = {"container": "backend", "memory_limit": "256Mi"}


def test_patch_is_valid_strategic_merge_yaml():
    doc = yaml.safe_load(build_resources_patch("backend", "backend", "256Mi"))
    assert doc["kind"] == "Deployment" and doc["metadata"]["name"] == "backend"
    assert doc["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "256Mi"


def call(policy, tmp_path, dry_run, enabled, transport=None, ns="skillpulse-stg"):
    s = Settings(audit_dir=str(tmp_path), github_enabled=enabled, github_token="t", github_repo="o/r")
    return propose_config_change(settings=s, policy=policy, incident_id="inc-1", namespace=ns, workload="backend",
                                 params=PARAMS, rationale="because", dry_run=dry_run, transport=transport)


def test_dry_run_only_writes_a_local_proposal(policy, tmp_path):
    r = call(policy, tmp_path, True, True)
    assert r["mode"] == "proposal_file" and (tmp_path / "proposals" / "inc-1.patch.yaml").exists()


def test_disabled_github_never_calls_network(policy, tmp_path):
    def boom(req):
        raise AssertionError("network used")
    r = call(policy, tmp_path, False, False, httpx.MockTransport(boom))
    assert r["mode"] == "proposal_file"


def test_pull_request_flow(policy, tmp_path):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        p = req.url.path
        if req.method == "GET" and p.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if req.method == "POST" and p.endswith("/git/refs"):
            assert json.loads(req.content)["ref"] == "refs/heads/aiops/inc-1"
            return httpx.Response(201, json={})
        if req.method == "GET" and "/contents/" in p:
            return httpx.Response(200, json={"sha": "filesha"})
        if req.method == "PUT":
            body = json.loads(req.content)
            assert body["sha"] == "filesha" and body["branch"] == "aiops/inc-1"
            assert "256Mi" in base64.b64decode(body["content"]).decode()
            assert p.endswith("k8s/gitops/stg/patches/backend-resources.yaml")      # allowlisted path only
            return httpx.Response(200, json={})
        if req.method == "POST" and p.endswith("/pulls"):
            assert json.loads(req.content)["base"] == "main"
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/9"})
        return httpx.Response(404)
    r = call(policy, tmp_path, False, True, httpx.MockTransport(handler))
    assert r["mode"] == "pull_request" and r["url"].endswith("/pull/9")
    assert calls[-1] == ("POST", "/repos/o/r/pulls")


def test_unconfigured_target_refused(policy, tmp_path):
    import pytest
    s = Settings(audit_dir=str(tmp_path))
    with pytest.raises(ValueError):
        propose_config_change(settings=s, policy=policy, incident_id="i", namespace="skillpulse-stg",
                              workload="mysql", params=PARAMS, rationale="", dry_run=True)
