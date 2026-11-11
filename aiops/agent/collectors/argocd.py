"""Read-only view of one Argo CD Application (get on applications.argoproj.io, resourceNames-scoped).

The agent never syncs, patches or refreshes an Application: it only observes what Argo CD did after a human merged.
"""
from __future__ import annotations

from ..util import valid_name


class ArgoClient:
    def __init__(self, namespace: str = "argocd", api=None):
        if api is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            api = client.CustomObjectsApi()
        self.namespace, self.api = namespace, api

    def get(self, app: str) -> dict:
        if not valid_name(app):
            raise ValueError("invalid Argo CD application name")
        o = self.api.get_namespaced_custom_object("argoproj.io", "v1alpha1", self.namespace, "applications", app)
        return summarize_application(o)


def summarize_application(o: dict) -> dict:
    st = o.get("status", {}) or {}
    op = st.get("operationState", {}) or {}
    return {
        "sync_status": (st.get("sync", {}) or {}).get("status"),
        "sync_revision": (st.get("sync", {}) or {}).get("revision"),
        "health": (st.get("health", {}) or {}).get("status"),
        "operation_phase": op.get("phase"),
        "operation_started_at": op.get("startedAt"),
        "operation_finished_at": op.get("finishedAt"),
        "operation_revision": (op.get("syncResult", {}) or {}).get("revision"),
        "auto_sync": bool((o.get("spec", {}).get("syncPolicy", {}) or {}).get("automated")),
        "prune": bool(((o.get("spec", {}).get("syncPolicy", {}) or {}).get("automated") or {}).get("prune")),
    }
