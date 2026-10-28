"""Webhook + approval API (FastAPI)."""
from __future__ import annotations

import hmac
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from .graph import new_incident_id, resume_incident, run_incident
from .ingest import IngestError, parse_webhook
from .util import iso, parse_ts, utcnow

MAX_BODY = 512 * 1024


def create_app(deps, graph) -> FastAPI:
    s = deps.settings
    app = FastAPI(title="SkillPulse AIOps agent")
    pool = ThreadPoolExecutor(max_workers=2)
    recent: dict[str, tuple[str, float]] = {}   # "ns/workload" -> (incident_id, time)
    lock = threading.Lock()

    def check(token: str, supplied: str | None):
        if not token:
            raise HTTPException(503, "token not configured on server")
        got = (supplied or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got.encode(), token.encode()):
            raise HTTPException(401, "unauthorized")

    def webhook_auth(authorization: str | None = Header(default=None)):
        check(s.webhook_token, authorization)

    def approval_auth(authorization: str | None = Header(default=None)):
        check(s.approval_token, authorization)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "mode": s.mode, "dry_run": s.dry_run, "reasoner": deps.reasoner.name}

    @app.post("/webhook/grafana", status_code=202, dependencies=[Depends(webhook_auth)])
    async def webhook(request: Request):
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "payload too large")
        try:
            alerts = parse_webhook(json.loads(raw))
        except (ValueError, IngestError) as e:
            raise HTTPException(400, str(e)) from e
        accepted, deduped = [], []
        import time
        for a in alerts:
            key = f"{a['namespace']}/{a['workload_hint']}"
            with lock:
                prev = recent.get(key)
                if a["status"] == "firing" and prev and time.time() - prev[1] < s.dedupe_window_s:
                    deps.audit.record(prev[0], "alert_deduplicated", "aiops-agent", {"alert": a["name"], "key": key})
                    deduped.append(prev[0])
                    continue
                iid = new_incident_id()
                if a["status"] == "firing":
                    recent[key] = (iid, time.time())
            pool.submit(run_incident, graph, a, iid)
            accepted.append(iid)
        return {"accepted": accepted, "deduplicated_into": deduped}

    def incident_file(iid: str) -> Path:
        if not iid.startswith("inc-") or "/" in iid or ".." in iid:
            raise HTTPException(400, "bad incident id")
        return Path(s.audit_dir) / "incidents" / f"{iid}.json"

    @app.get("/incidents", dependencies=[Depends(webhook_auth)])
    def list_incidents():
        d = Path(s.audit_dir) / "incidents"
        return sorted(p.stem for p in d.glob("inc-*.json")) if d.exists() else []

    @app.get("/incidents/{iid}/report", dependencies=[Depends(webhook_auth)])
    def report(iid: str):
        p = Path(s.audit_dir) / "reports" / f"{incident_file(iid).stem}.md"
        if not p.exists():
            raise HTTPException(404, "no such report")
        return {"report": p.read_text()}

    @app.post("/incidents/{iid}/decision", dependencies=[Depends(approval_auth)])
    async def decide(iid: str, request: Request):
        body = await request.json()
        decision, approver = body.get("decision"), str(body.get("approver", "")).strip()
        if decision not in ("approve", "reject") or not approver:
            raise HTTPException(400, "need decision=approve|reject and approver")
        incident_file(iid)
        try:
            state = resume_incident(graph, iid, decision, approver)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"status": state.get("status"), "outcome": state.get("outcome")}

    return app
