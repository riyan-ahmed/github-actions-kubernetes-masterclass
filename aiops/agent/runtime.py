"""Wiring: build the production dependency set and the compiled graph from Settings."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .audit import AuditLog
from .collectors.k8s import KubeClient
from .collectors.loki import LokiClient
from .collectors.prometheus import PrometheusClient
from .config import Settings
from .graph import Deps, build_graph
from .llm import build_reasoner
from .policy import Policy
from .retrieval import RunbookIndex


def build_runtime(settings: Settings, kube=None):
    Path(settings.audit_dir).mkdir(parents=True, exist_ok=True)
    deps = Deps(settings=settings, policy=Policy.load(settings.policy_file),
                audit=AuditLog(settings.audit_dir), kube=kube or KubeClient(),
                prom=PrometheusClient(settings.prometheus_url), loki=LokiClient(settings.loki_url),
                index=RunbookIndex.from_dir(settings.runbook_dir), reasoner=build_reasoner(settings))
    from langgraph.checkpoint.sqlite import SqliteSaver
    conn = sqlite3.connect(str(Path(settings.audit_dir) / "checkpoints.sqlite"), check_same_thread=False)
    return deps, build_graph(deps, SqliteSaver(conn))
