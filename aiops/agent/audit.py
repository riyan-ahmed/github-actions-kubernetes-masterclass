"""Append-only, hash-chained audit log (JSON Lines).

Every record embeds the SHA-256 of the previous record, so editing or deleting a line
is detectable with verify_chain(). All payloads are redacted before being written.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .redact import redact_obj
from .util import utcnow

GENESIS = "0" * 64


def _digest(prev: str, body: dict) -> str:
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + canon).encode()).hexdigest()


class AuditLog:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "audit.jsonl"
        self._lock = threading.Lock()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def record(self, incident_id: str | None, event: str, actor: str, data: dict | None = None) -> dict:
        with self._lock:
            body = {
                "ts": utcnow().isoformat(),
                "incident_id": incident_id,
                "event": event,
                "actor": actor,
                "data": redact_obj(data or {}),
            }
            prev = self._last_hash()
            rec = {**body, "prev_hash": prev, "hash": _digest(prev, body)}
            with self.path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
            return rec

    def events(self, incident_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    if incident_id is None or rec["incident_id"] == incident_id:
                        out.append(rec)
        return out

    def verify_chain(self) -> tuple[bool, str]:
        prev = GENESIS
        for n, rec in enumerate(self.events(), 1):
            body = {k: rec[k] for k in ("ts", "incident_id", "event", "actor", "data")}
            if rec["prev_hash"] != prev or rec["hash"] != _digest(prev, body):
                return False, f"chain broken at record {n}"
            prev = rec["hash"]
        return True, "ok"

    def count_executions(self, namespace: str, workload: str, within: timedelta = timedelta(hours=1)) -> int:
        """Number of real (non dry-run) executions against a workload; used for rate limiting."""
        since = utcnow() - within
        n = 0
        for rec in self.events():
            if rec["event"] != "action_executed":
                continue
            d = rec["data"]
            if d.get("namespace") == namespace and d.get("workload") == workload and not d.get("dry_run"):
                if datetime.fromisoformat(rec["ts"]) >= since:
                    n += 1
        return n
