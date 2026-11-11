"""Alert ingestion: parse Grafana / Alertmanager webhook payloads into validated alerts.

The payload is untrusted. Only validated DNS-style names are ever used to select a
target; free text is sanitised, truncated and scanned for injection attempts.
"""
from __future__ import annotations

import re
from typing import Any

from .redact import detect_injection, sanitize_untrusted
from .util import valid_name

MAX_ALERTS = 50
_LABEL_KEEP = ("alertname", "namespace", "pod", "container", "deployment", "workload", "severity", "app")
_POD_HASH = re.compile(r"^(?P<dep>.+)-[a-z0-9]{6,10}-[a-z0-9]{5}$")


class IngestError(ValueError):
    pass


def workload_hint(labels: dict[str, str]) -> str | None:
    """Best-effort workload name from labels; the real owner is resolved via the K8s API later."""
    for key in ("deployment", "workload"):
        if valid_name(labels.get(key)):
            return labels[key]
    pod = labels.get("pod")
    if valid_name(pod):
        m = _POD_HASH.match(pod)
        return m.group("dep") if m else pod
    return None


def parse_webhook(payload: Any) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
        raise IngestError("payload must be an object with an 'alerts' list")
    if len(payload["alerts"]) > MAX_ALERTS:
        raise IngestError(f"too many alerts (max {MAX_ALERTS})")

    alerts = []
    for raw in payload["alerts"]:
        if not isinstance(raw, dict):
            continue
        status = raw.get("status", payload.get("status", "firing"))
        if status not in ("firing", "resolved"):
            continue
        raw_labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
        labels = {k: sanitize_untrusted(raw_labels[k], 120) for k in _LABEL_KEEP if k in raw_labels}
        annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
        text = " ".join(str(annotations.get(k, "")) for k in ("summary", "description"))
        namespace = labels.get("namespace")
        alerts.append({
            "name": labels.get("alertname", "unknown")[:100],
            "status": status,
            "severity": labels.get("severity", "unknown")[:20],
            "namespace": namespace if valid_name(namespace) else None,
            "workload_hint": workload_hint(labels),
            "pod": labels.get("pod") if valid_name(labels.get("pod")) else None,
            "labels": labels,
            "summary": sanitize_untrusted(text, 300),
            "starts_at": raw.get("startsAt"),
            "fingerprint": sanitize_untrusted(raw.get("fingerprint", ""), 64),
            "source": "grafana-webhook",
            "injection_flags": detect_injection(text) + [
                m for v in raw_labels.values() if isinstance(v, str) for m in detect_injection(v)
            ],
        })
    return alerts


def manual_alert(namespace: str, workload: str, name: str = "ManualTrigger") -> dict:
    """Alert built from CLI arguments (evidence is still collected live)."""
    if not (valid_name(namespace) and valid_name(workload)):
        raise IngestError("invalid namespace or workload name")
    return {
        "name": name, "status": "firing", "severity": "unknown", "namespace": namespace,
        "workload_hint": workload, "pod": None, "labels": {"namespace": namespace, "deployment": workload},
        "summary": "manually triggered diagnosis", "starts_at": None, "fingerprint": "",
        "source": "cli", "injection_flags": [],
    }
