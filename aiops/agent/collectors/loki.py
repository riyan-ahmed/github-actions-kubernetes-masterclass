"""Loki HTTP API log evidence. Log lines are untrusted and are sanitised on the way in."""
from __future__ import annotations

import time

import httpx

from ..redact import detect_injection, sanitize_untrusted
from ..util import valid_name


class LokiClient:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 10):
        self.http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout)

    def collect(self, namespace: str, pods: list[str], lookback_min: int = 15, limit: int = 60) -> dict:
        if not valid_name(namespace) or not all(valid_name(p) for p in pods):
            raise ValueError("refusing to build a query from an invalid name")
        if not pods:
            return {"query": None, "lines": [], "injection_flags": []}
        logql = f'{{namespace="{namespace}", pod=~"{"|".join(pods)}"}}'
        end = time.time_ns()
        r = self.http.get("/loki/api/v1/query_range", params={
            "query": logql, "limit": limit, "direction": "backward",
            "start": end - lookback_min * 60 * 10**9, "end": end})
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"loki error: {body}")
        entries = []
        for stream in body["data"]["result"]:
            for ts, line in stream["values"]:
                entries.append((int(ts), stream["stream"].get("pod", ""), line))
        entries.sort(reverse=True)
        raw_lines = [l for _, _, l in entries]
        return {
            "query": logql,
            "lines": [{"pod": p, "line": sanitize_untrusted(l, 300)} for _, p, l in entries[:limit]],
            "injection_flags": sorted({f for l in raw_lines for f in detect_injection(l)}),
        }
