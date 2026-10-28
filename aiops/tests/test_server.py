import time

import pytest
from fastapi.testclient import TestClient

from aiops.agent.server import create_app
from aiops.tests import fakes

PAYLOAD = {"alerts": [{"status": "firing", "startsAt": "2026-10-27T11:58:00Z",
                       "labels": {"alertname": "SkillPulseCrashLooping", "namespace": "skillpulse-stg",
                                  "pod": f"{fakes.DEP}-rev2-abcde"}}]}
H = {"Authorization": "Bearer wh-token"}


@pytest.fixture
def client(make_deps):
    deps, graph, apps = make_deps("crash", mode="approval", dry_run=True, prom_crash=True)
    object.__setattr__(deps.settings, "webhook_token", "wh-token")
    object.__setattr__(deps.settings, "approval_token", "ap-token")
    return TestClient(create_app(deps, graph)), deps


def wait_for(deps, iid, event, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if any(e["event"] == event for e in deps.audit.events(iid)):
            return
        time.sleep(0.05)
    raise AssertionError(f"{event} not seen")


def test_health_is_open_and_shows_safe_defaults(client):
    c, _ = client
    j = c.get("/healthz").json()
    assert j["mode"] == "approval" and j["dry_run"] is True and j["reasoner"] == "deterministic-rules"


def test_webhook_requires_token(client):
    c, _ = client
    assert c.post("/webhook/grafana", json=PAYLOAD).status_code == 401
    assert c.post("/webhook/grafana", json=PAYLOAD, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_webhook_rejects_bad_payloads(client):
    c, _ = client
    assert c.post("/webhook/grafana", content=b"not json", headers=H).status_code == 400
    assert c.post("/webhook/grafana", json={"alerts": "x"}, headers=H).status_code == 400
    assert c.post("/webhook/grafana", content=b"x" * 600_000, headers=H).status_code == 413


def test_alert_flows_to_pending_approval_and_decision_endpoint(client):
    c, deps = client
    r = c.post("/webhook/grafana", json=PAYLOAD, headers=H)
    assert r.status_code == 202
    [iid] = r.json()["accepted"]
    wait_for(deps, iid, "approval_requested")
    assert c.get(f"/incidents/{iid}/report", headers=H).json()["report"].startswith(f"# Incident {iid}")
    # approval uses a different token than the webhook
    body = {"decision": "approve", "approver": "alice"}
    assert c.post(f"/incidents/{iid}/decision", json=body, headers=H).status_code == 401
    ok = c.post(f"/incidents/{iid}/decision", json=body, headers={"Authorization": "Bearer ap-token"})
    assert ok.status_code == 200 and "dry-run" in ok.json()["outcome"]
    assert c.post(f"/incidents/{iid}/decision", json=body, headers={"Authorization": "Bearer ap-token"}).status_code == 409


def test_duplicate_alerts_join_the_open_incident(client):
    c, deps = client
    first = c.post("/webhook/grafana", json=PAYLOAD, headers=H).json()["accepted"][0]
    second = c.post("/webhook/grafana", json=PAYLOAD, headers=H).json()
    assert second["accepted"] == [] and second["deduplicated_into"] == [first]
    wait_for(deps, first, "alert_deduplicated")


def test_incident_id_path_traversal_rejected(client):
    c, _ = client
    assert c.get("/incidents/..%2f..%2fetc/report", headers=H).status_code in (400, 404)
    assert c.post("/incidents/x/decision", json={"decision": "approve", "approver": "a"},
                  headers={"Authorization": "Bearer ap-token"}).status_code == 400
