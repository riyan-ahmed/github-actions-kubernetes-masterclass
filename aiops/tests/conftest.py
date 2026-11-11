import pytest

from aiops.agent.audit import AuditLog
from aiops.agent.collectors.loki import LokiClient
from aiops.agent.collectors.prometheus import PrometheusClient
from aiops.agent.config import Settings
from aiops.agent.graph import Deps, build_graph
from aiops.agent.llm import DeterministicReasoner
from aiops.agent.policy import Policy
from aiops.agent.retrieval import RunbookIndex
from aiops.tests import fakes  # noqa: F401

import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def policy():
    return Policy.load(ROOT / "policies" / "policy.yaml")


@pytest.fixture
def make_deps(tmp_path, policy):
    def _make(scenario, *, mode="diagnose", dry_run=True, history=True, prev_ready=True, labels=None,
              log_lines=(), reasoner=None, loki_status=200, prom_oom=False, prom_crash=False):
        kube, apps = fakes.make_kube(scenario, history, labels)
        settings = Settings(mode=mode, dry_run=dry_run, audit_dir=str(tmp_path / "audit"),
                            verify_timeout_s=30, verify_interval_s=1)
        deps = Deps(settings=settings, policy=policy, audit=AuditLog(settings.audit_dir), kube=kube,
                    prom=PrometheusClient("http://prom", fakes.prom_transport(prev_ready, oom=prom_oom, crash=prom_crash)),
                    loki=LokiClient("http://loki", fakes.loki_transport(log_lines, loki_status)),
                    index=RunbookIndex.from_dir(ROOT / "runbooks"),
                    reasoner=reasoner or DeterministicReasoner(), sleep=lambda s: None)
        return deps, build_graph(deps), apps
    return _make


def alert(workload=fakes.DEP, ns=fakes.NS, name="SkillPulsePodNotReady", status="firing", pod=None):
    return {"name": name, "status": status, "severity": "warning", "namespace": ns, "workload_hint": workload,
            "pod": pod, "labels": {}, "summary": "", "starts_at": "2026-10-27T11:58:00Z", "fingerprint": "f",
            "source": "test", "injection_flags": []}
