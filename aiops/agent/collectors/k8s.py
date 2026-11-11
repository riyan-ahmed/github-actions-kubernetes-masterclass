"""Read-only Kubernetes evidence collection (and the few allowlisted write primitives).

Secrets and ConfigMaps are never read. Env var values are only kept for literal,
non-sensitive variables; anything from secretKeyRef is reduced to its name.
"""
from __future__ import annotations

import re
from typing import Any

from ..redact import sanitize_untrusted
from ..util import iso

REVISION_ANN = "deployment.kubernetes.io/revision"
_SENSITIVE_ENV = re.compile(r"pass|secret|token|key|credential|dsn", re.I)


class KubeError(RuntimeError):
    pass


def _state(s) -> dict | None:
    if s is None:
        return None
    for kind in ("waiting", "running", "terminated"):
        v = getattr(s, kind, None)
        if v is not None:
            d = {"kind": kind}
            for attr in ("reason", "exit_code", "message"):
                if getattr(v, attr, None) is not None:
                    d[attr] = sanitize_untrusted(getattr(v, attr), 200) if attr != "exit_code" else v.exit_code
            if kind == "terminated":
                d["finished_at"] = iso(v.finished_at)
            return d
    return None


def template_summary(template) -> dict:
    """Compact, secret-free description of a pod template used for revision diffs."""
    out: dict[str, Any] = {"containers": {}}
    for c in template.spec.containers or []:
        res = c.resources
        env = {}
        for e in c.env or []:
            if e.value_from is not None:
                env[e.name] = "<from secret/configmap>"
            elif _SENSITIVE_ENV.search(e.name):
                env[e.name] = "<redacted>"
            else:
                env[e.name] = sanitize_untrusted(e.value, 80)
        probe = c.readiness_probe
        path = None
        if probe is not None and probe.http_get is not None:
            path = probe.http_get.path
        out["containers"][c.name] = {
            "image": c.image,
            "memory_limit": (res.limits or {}).get("memory") if res and res.limits else None,
            "memory_request": (res.requests or {}).get("memory") if res and res.requests else None,
            "readiness_path": path,
            "env": env,
        }
    return out


def diff_summaries(prev: dict, cur: dict) -> list[str]:
    """Human-readable differences between two template_summary() results."""
    changes = []
    for name, c in cur["containers"].items():
        p = prev["containers"].get(name)
        if p is None:
            changes.append(f"container {name}: added")
            continue
        for key in ("image", "memory_limit", "memory_request", "readiness_path"):
            if p[key] != c[key]:
                changes.append(f"{name}.{key}: {p[key]} -> {c[key]}")
        for ev in sorted(set(p["env"]) | set(c["env"])):
            if p["env"].get(ev) != c["env"].get(ev):
                changes.append(f"{name}.env.{ev}: {p['env'].get(ev)} -> {c['env'].get(ev)}")
    return changes


class KubeClient:
    def __init__(self, core=None, apps=None):
        if core is None or apps is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            core, apps = client.CoreV1Api(), client.AppsV1Api()
        self.core, self.apps = core, apps

    # ---- target resolution -------------------------------------------------
    def resolve_workload(self, namespace: str, pod: str | None, hint: str | None) -> dict | None:
        """Resolve to a Deployment using ownerReferences (pod -> ReplicaSet -> Deployment)."""
        name = hint
        if pod:
            try:
                p = self.core.read_namespaced_pod(pod, namespace)
                for o in p.metadata.owner_references or []:
                    if o.kind == "ReplicaSet":
                        rs = self.apps.read_namespaced_replica_set(o.name, namespace)
                        for ro in rs.metadata.owner_references or []:
                            if ro.kind == "Deployment":
                                name = ro.name
                    elif o.kind in ("StatefulSet", "DaemonSet", "Job"):
                        return {"kind": o.kind, "name": o.name, "namespace": namespace}
            except Exception:
                pass  # pod may be gone; fall back to hint
        if not name:
            return None
        try:
            dep = self.apps.read_namespaced_deployment(name, namespace)
        except Exception:
            try:
                self.apps.read_namespaced_stateful_set(name, namespace)
                return {"kind": "StatefulSet", "name": name, "namespace": namespace}
            except Exception:
                return None
        labels = dep.metadata.labels or {}
        anns = dep.metadata.annotations or {}
        managed = "argocd.argoproj.io/instance" in labels or "argocd.argoproj.io/tracking-id" in anns
        return {"kind": "Deployment", "name": name, "namespace": namespace, "gitops_managed": managed}

    # ---- evidence ------------------------------------------------------------
    def collect(self, namespace: str, name: str, log_tail: int = 40) -> dict:
        dep = self.apps.read_namespaced_deployment(name, namespace)
        selector = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
        revisions = self.revisions(namespace, dep)
        pods_raw = self.core.list_namespaced_pod(namespace, label_selector=selector).items
        pods = [self._pod(p) for p in pods_raw]
        cur_rev = next((r for r in revisions if r["current"]), None)
        prev_rev = next((r for r in revisions if not r["current"] and cur_rev and r["revision"] < cur_rev["revision"]), None)
        diff = diff_summaries(prev_rev["template"], cur_rev["template"]) if cur_rev and prev_rev else []
        names = {name, *(p["name"] for p in pods), *(r["name"] for r in revisions)}
        data = {
            "deployment": {
                "name": name,
                "replicas_desired": dep.spec.replicas,
                "replicas_ready": dep.status.ready_replicas or 0,
                "replicas_available": dep.status.available_replicas or 0,
                "replicas_updated": dep.status.updated_replicas or 0,
                "generation": dep.metadata.generation,
                "observed_generation": dep.status.observed_generation,
                "conditions": [{"type": c.type, "status": c.status, "reason": c.reason,
                                "message": sanitize_untrusted(c.message, 200)} for c in dep.status.conditions or []],
                "current_revision": cur_rev["revision"] if cur_rev else None,
            },
            "pods": pods,
            "revisions": [{k: v for k, v in r.items() if k != "template"} | {"template": r["template"]} for r in revisions],
            "revision_diff": diff,
            "events": self.events(namespace, names),
            "previous_logs": {},
        }
        for p in pods:
            for c in p["containers"]:
                if c["restart_count"] > 0 or c["state"] and c["state"]["kind"] == "waiting":
                    data["previous_logs"][f"{p['name']}/{c['name']}"] = self.pod_logs(namespace, p["name"], c["name"], log_tail)
        return data

    def _pod(self, p) -> dict:
        cs = []
        for c in p.status.container_statuses or []:
            cs.append({"name": c.name, "ready": c.ready, "restart_count": c.restart_count,
                       "image": c.image, "state": _state(c.state), "last_state": _state(c.last_state)})
        return {"name": p.metadata.name, "phase": p.status.phase, "node": p.spec.node_name,
                "ready": any(c.type == "Ready" and c.status == "True" for c in p.status.conditions or []),
                "created": iso(p.metadata.creation_timestamp), "containers": cs}

    def revisions(self, namespace: str, dep) -> list[dict]:
        cur = int((dep.metadata.annotations or {}).get(REVISION_ANN, 0))
        out = []
        for rs in self.apps.list_namespaced_replica_set(namespace).items:
            if not any(o.uid == dep.metadata.uid for o in rs.metadata.owner_references or []):
                continue
            rev = int((rs.metadata.annotations or {}).get(REVISION_ANN, 0))
            out.append({"name": rs.metadata.name, "revision": rev, "current": rev == cur,
                        "created": iso(rs.metadata.creation_timestamp),
                        "replicas": rs.status.replicas or 0, "ready": rs.status.ready_replicas or 0,
                        "template": template_summary(rs.spec.template)})
        return sorted(out, key=lambda r: r["revision"], reverse=True)

    def events(self, namespace: str, names: set[str], limit: int = 25) -> list[dict]:
        items = [e for e in self.core.list_namespaced_event(namespace).items if e.involved_object.name in names]
        items.sort(key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp, reverse=True)
        return [{"type": e.type, "reason": e.reason, "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                 "message": sanitize_untrusted(e.message, 300), "count": e.count,
                 "last_seen": iso(e.last_timestamp or e.event_time)} for e in items[:limit]]

    def pod_logs(self, namespace: str, pod: str, container: str, tail: int) -> list[str]:
        """Kubernetes-API log fallback (previous container instance first)."""
        for previous in (True, False):
            try:
                text = self.core.read_namespaced_pod_log(pod, namespace, container=container,
                                                         previous=previous, tail_lines=tail)
                if text:
                    return [sanitize_untrusted(l, 300) for l in text.splitlines()[-tail:]]
            except Exception:
                continue
        return []

    # ---- allowlisted write primitives (called only from actions.py after policy) ----
    def previous_healthy_revision(self, namespace: str, name: str) -> dict:
        """Return the newest ReplicaSet older than the current one that has ever run ready pods."""
        dep = self.apps.read_namespaced_deployment(name, namespace)
        revs = self.revisions(namespace, dep)
        cur = next((r for r in revs if r["current"]), None)
        if cur is None:
            raise KubeError("current revision not found")
        older = [r for r in revs if r["revision"] < cur["revision"]]
        if not older:
            raise KubeError("no previous revision exists")
        return older[0] | {"deployment_uid": dep.metadata.uid, "current_revision": cur["revision"]}

    def rollout_undo(self, namespace: str, name: str, dry_run: bool) -> dict:
        from kubernetes import client
        dep = self.apps.read_namespaced_deployment(name, namespace)
        cur = int((dep.metadata.annotations or {}).get(REVISION_ANN, 0))
        target = None
        for rs in self.apps.list_namespaced_replica_set(namespace).items:
            if not any(o.uid == dep.metadata.uid for o in rs.metadata.owner_references or []):
                continue
            rev = int((rs.metadata.annotations or {}).get(REVISION_ANN, 0))
            if rev < cur and (target is None or rev > target[0]):
                target = (rev, rs)
        if target is None:
            raise KubeError("no previous ReplicaSet to roll back to")
        rev, rs = target
        template = client.ApiClient().sanitize_for_serialization(rs.spec.template)
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        patch = [{"op": "replace", "path": "/spec/template", "value": template}]
        self.apps.patch_namespaced_deployment(name, namespace, patch, dry_run="All" if dry_run else None)
        return {"from_revision": cur, "to_revision": rev, "to_replicaset": rs.metadata.name}

    def restart(self, namespace: str, name: str, ts: str, dry_run: bool) -> dict:
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": ts}}}}}
        self.apps.patch_namespaced_deployment(name, namespace, body, dry_run="All" if dry_run else None)
        return {"restartedAt": ts}
