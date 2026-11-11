"""Prometheus HTTP API evidence. Every result carries the PromQL that produced it."""
from __future__ import annotations

import httpx

from ..util import valid_name


class PrometheusClient:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 10):
        self.http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout)

    def query(self, promql: str) -> list[dict]:
        r = self.http.get("/api/v1/query", params={"query": promql})
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"prometheus error: {body.get('error')}")
        return [{"metric": s["metric"], "value": float(s["value"][1]), "ts": s["value"][0]}
                for s in body["data"]["result"]]

    def collect(self, namespace: str, pods: list[str], lookback_min: int = 15) -> dict:
        if not valid_name(namespace) or not all(valid_name(p) for p in pods):
            raise ValueError("refusing to build a query from an invalid name")
        if not pods:
            return {}
        sel = f'namespace="{namespace}",pod=~"{"|".join(pods)}"'
        w = f"{lookback_min}m"
        queries = {
            "restarts_in_window": f"sum by (pod,container) (increase(kube_pod_container_status_restarts_total{{{sel}}}[{w}]))",
            "waiting_reason": f"kube_pod_container_status_waiting_reason{{{sel}}} == 1",
            "last_terminated_reason": f"kube_pod_container_status_last_terminated_reason{{{sel}}} == 1",
            "pod_ready": f'kube_pod_status_ready{{{sel},condition="true"}}',
            "memory_working_set_bytes": f'max by (pod,container) (container_memory_working_set_bytes{{{sel},container!="",container!="POD"}})',
            "memory_limit_bytes": f'max by (pod,container) (kube_pod_container_resource_limits{{{sel},resource="memory"}})',
        }
        out = {}
        for name, q in queries.items():
            try:
                out[name] = {"query": q, "result": self.query(q)}
            except Exception as e:  # partial evidence is still useful
                out[name] = {"query": q, "error": f"{type(e).__name__}: {e}"}
        return out

    def was_ready(self, namespace: str, replicaset: str, window: str = "24h") -> dict:
        """Did any pod of this ReplicaSet ever report Ready within the retention window?

        This is the evidence that a rollback target is a known-good revision rather than
        merely an older one.
        """
        if not valid_name(namespace) or not valid_name(replicaset):
            raise ValueError("refusing to build a query from an invalid name")
        q = (f'max(max_over_time(kube_pod_status_ready{{namespace="{namespace}",'
             f'pod=~"{replicaset}-[a-z0-9]{{5}}",condition="true"}}[{window}]))')
        res = self.query(q)
        return {"query": q, "was_ready": bool(res and res[0]["value"] >= 1), "window": window}
