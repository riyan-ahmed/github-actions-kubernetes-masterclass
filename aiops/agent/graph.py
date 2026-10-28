"""The LangGraph workflow.

alert -> identify -> k8s evidence -> metrics -> logs -> runbook -> diagnose -> propose
      -> policy -> [approval gate] -> execute -> verify -> record

Each node reads and returns slices of IncidentState. Failing collectors degrade the
evidence (recorded under evidence.errors) rather than aborting the incident.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .actions import execute_action
from .audit import AuditLog
from .classify import classify, propose_action
from .config import Settings
from .llm import compact_evidence
from .models import IncidentState
from .policy import Policy, PolicyError
from .redact import sanitize_untrusted
from .report import render_report
from .retrieval import RunbookIndex, build_query
from .util import iso, parse_ts, utcnow, valid_name
from .verify import wait_for_recovery

AGENT = "aiops-agent"


@dataclass
class Deps:
    settings: Settings
    policy: Policy
    audit: AuditLog
    kube: Any
    prom: Any            # PrometheusClient or None
    loki: Any            # LokiClient or None
    index: RunbookIndex
    reasoner: Any
    sleep: Any = None    # injectable for tests


def new_incident_id() -> str:
    return utcnow().strftime("inc-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def build_graph(d: Deps, checkpointer=None):
    s, audit = d.settings, d.audit

    def elapsed(state) -> dict:
        return dict(state.get("timings", {}))

    def ev_with(state, **kw) -> dict:
        return {**state.get("evidence", {}), **kw}

    def add_error(state, msg) -> list:
        return state.get("evidence", {}).get("errors", []) + [msg]

    # ---------------------------------------------------------------- nodes
    def identify(state: IncidentState):
        alert = state["alert"]
        iid = state["incident_id"]
        now = utcnow()
        timings = {"agent_received_at": iso(now)}
        starts = parse_ts(alert.get("starts_at"))
        if starts:
            timings["alert_start_to_agent_s"] = round((now - starts).total_seconds(), 1)
        ns = alert.get("namespace")
        audit.record(iid, "alert_received", AGENT, {"alert": alert})
        if not ns or not d.policy.monitored(ns):
            audit.record(iid, "incident_ignored", AGENT, {"reason": f"namespace {ns!r} not monitored"})
            return {"status": "ignored", "outcome": "ignored: namespace not monitored", "timings": timings}
        if alert["status"] == "resolved":
            audit.record(iid, "incident_ignored", AGENT, {"reason": "alert resolved"})
            return {"status": "ignored", "outcome": "ignored: alert resolved", "timings": timings}
        target = d.kube.resolve_workload(ns, alert.get("pod"), alert.get("workload_hint"))
        if not target:
            audit.record(iid, "incident_ignored", AGENT, {"reason": "workload could not be resolved"})
            return {"status": "ignored", "outcome": "ignored: workload not found", "timings": timings}
        audit.record(iid, "incident_identified", AGENT, {"target": target})
        return {"target": target, "status": "collecting", "timings": timings, "evidence": {"errors": []}}

    def collect_k8s(state: IncidentState):
        t = state["target"]
        if t["kind"] != "Deployment":
            audit.record(state["incident_id"], "evidence_unsupported_kind", AGENT, {"kind": t["kind"]})
            return {"evidence": ev_with(state, k8s={}, errors=add_error(state, f"kind {t['kind']} is diagnosed from metrics/logs only"))}
        try:
            k8s = d.kube.collect(t["namespace"], t["name"])
            audit.record(state["incident_id"], "evidence_collected", AGENT,
                         {"source": "kubernetes", "pods": len(k8s["pods"]), "events": len(k8s["events"])})
            return {"evidence": ev_with(state, k8s=k8s)}
        except Exception as e:
            audit.record(state["incident_id"], "evidence_failed", AGENT, {"source": "kubernetes", "error": sanitize_untrusted(e, 200)})
            return {"evidence": ev_with(state, k8s={}, errors=add_error(state, f"kubernetes: {type(e).__name__}"))}

    def collect_metrics(state: IncidentState):
        t, k8s = state["target"], state["evidence"].get("k8s", {})
        pods = [p["name"] for p in k8s.get("pods", [])]
        metrics, errors = {}, state["evidence"].get("errors", [])
        if d.prom is None:
            errors = errors + ["prometheus: not configured"]
        else:
            try:
                metrics = d.prom.collect(t["namespace"], pods, s.lookback_minutes)
                revs = k8s.get("revisions", [])
                prev = next((r for r in revs if not r["current"]), None)
                if prev:
                    metrics["previous_revision"] = {"replicaset": prev["name"],
                                                    **d.prom.was_ready(t["namespace"], prev["name"])}
                failed = [n for n, m in metrics.items() if isinstance(m, dict) and "error" in m]
                errors = errors + [f"prometheus query failed: {n}" for n in failed]
            except Exception as e:
                errors = errors + [sanitize_untrusted(f"prometheus: {type(e).__name__}: {e}", 120)]
        audit.record(state["incident_id"], "evidence_collected", AGENT,
                     {"source": "prometheus", "queries": [m.get("query") for m in metrics.values() if isinstance(m, dict)]})
        return {"evidence": ev_with(state, metrics=metrics, errors=errors)}

    def collect_logs(state: IncidentState):
        t, k8s = state["target"], state["evidence"].get("k8s", {})
        pods = [p["name"] for p in k8s.get("pods", [])]
        errors, logs = state["evidence"].get("errors", []), {}
        if d.loki is None:
            errors = errors + ["loki: not configured (using Kubernetes API logs)"]
        else:
            try:
                logs = d.loki.collect(t["namespace"], pods, s.lookback_minutes)
                if not logs["lines"]:
                    errors = errors + ["loki: no log lines returned (using Kubernetes API logs)"]
            except Exception as e:
                errors = errors + [sanitize_untrusted(f"loki: {type(e).__name__}: {e}", 120) + " (using Kubernetes API logs)"]
        audit.record(state["incident_id"], "evidence_collected", AGENT,
                     {"source": "loki", "lines": len(logs.get("lines", [])), "injection_flags": logs.get("injection_flags", [])})
        return {"evidence": ev_with(state, logs=logs, errors=errors)}

    def diagnose(state: IncidentState):
        ev = state["evidence"]
        cls = classify(ev)
        # Do not retrieve a runbook for an incident we could not classify: BM25 would still return
        # the "closest" document (e.g. CrashLoopBackOff for a healthy pod with one old restart).
        runbook = None if cls["incident_type"] == "unclassified" else d.index.best_runbook(build_query(cls, ev))
        diag = {"source": "rules", "explanation": "", "llm_error": None, "disagreement": False}
        try:
            r = d.reasoner.explain(cls, compact_evidence(cls, ev))
            diag.update(source=r["source"], explanation=r["explanation"])
            if r["source"] == "llm":
                diag["llm_incident_type"] = r["incident_type"]
                diag["disagreement"] = r["incident_type"] != cls["incident_type"]
        except Exception as e:
            diag["llm_error"] = sanitize_untrusted(f"{type(e).__name__}: {e}", 120)
            diag["explanation"] = cls["summary"]
        audit.record(state["incident_id"], "diagnosis", AGENT, {
            "classification": cls, "runbook": {k: runbook[k] for k in ("doc", "score")} if runbook else None,
            "reasoner": diag["source"], "disagreement": diag["disagreement"], "llm_error": diag["llm_error"]})
        t = elapsed(state)
        t["diagnosis_complete_at"] = iso(utcnow())
        return {"classification": cls, "runbook": runbook, "diagnosis": diag, "timings": t}

    def propose(state: IncidentState):
        prop = propose_action(state["classification"], state["evidence"], d.policy.memory_cap)
        audit.record(state["incident_id"], "action_proposed", AGENT, prop)
        return {"proposal": prop}

    def evaluate_policy(state: IncidentState):
        t, prop = state["target"], state["proposal"]
        decision = d.policy.evaluate(
            namespace=t["namespace"], workload=t["name"], kind=t["kind"], action_id=prop["action_id"],
            params=prop["params"], mode=s.mode, confidence=state["classification"]["confidence"],
            llm_disagrees=state["diagnosis"]["disagreement"], gitops_managed=t.get("gitops_managed", False),
            recent_executions=audit.count_executions(t["namespace"], t["name"]))
        audit.record(state["incident_id"], "policy_decision", "policy-engine",
                     {**decision.as_dict(), "mode": s.mode, "dry_run": s.dry_run, "action": prop["action_id"]})
        approval = {"status": "not_required" if decision.decision in ("advisory_only", "denied", "auto_allowed") else "pending"}
        return {"policy": {**decision.as_dict(), "mode": s.mode, "dry_run": s.dry_run}, "approval": approval}

    def route_after_policy(state: IncidentState) -> str:
        dec = state["policy"]["decision"]
        return {"requires_approval": "notify_pending", "auto_allowed": "execute"}.get(dec, "record")

    def notify_pending(state: IncidentState):
        path = _write_outputs(state | {"status": "awaiting_approval"})
        audit.record(state["incident_id"], "approval_requested", AGENT,
                     {"action": state["proposal"], "report": path})
        return {"status": "awaiting_approval", "report_path": path}

    def await_approval(state: IncidentState):
        answer = interrupt({"incident_id": state["incident_id"], "proposal": state["proposal"],
                            "policy": state["policy"]})
        ok = isinstance(answer, dict) and answer.get("decision") == "approve" and answer.get("approver")
        ap = {"status": "approved" if ok else "rejected",
              "approver": str((answer or {}).get("approver", ""))[:80], "at": iso(utcnow())}
        audit.record(state["incident_id"], "approval_" + ap["status"], ap["approver"] or "unknown", ap)
        return {"approval": ap}

    def route_after_approval(state: IncidentState) -> str:
        return "execute" if state["approval"]["status"] == "approved" else "record"

    def execute(state: IncidentState):
        iid, t, prop = state["incident_id"], state["target"], state["proposal"]
        # Defence in depth: re-evaluate policy right before acting, with fresh rate-limit data.
        decision = d.policy.evaluate(
            namespace=t["namespace"], workload=t["name"], kind=t["kind"], action_id=prop["action_id"],
            params=prop["params"], mode=s.mode, confidence=state["classification"]["confidence"],
            llm_disagrees=state["diagnosis"]["disagreement"], gitops_managed=t.get("gitops_managed", False),
            recent_executions=audit.count_executions(t["namespace"], t["name"]))
        if not decision.executable:
            audit.record(iid, "action_blocked", "policy-engine", decision.as_dict())
            return {"execution": {"status": "blocked", "detail": decision.as_dict()}}
        try:
            detail = execute_action(action_id=prop["action_id"], params=prop["params"], target=t, kube=d.kube,
                                    prom=d.prom, policy=d.policy, settings=s, incident_id=iid,
                                    rationale=prop["rationale"], dry_run=s.dry_run)
            audit.record(iid, "action_executed", AGENT, detail)
            t2 = elapsed(state)
            t2["action_at"] = iso(utcnow())
            return {"execution": {"status": "dry_run" if s.dry_run else "executed", "detail": detail}, "timings": t2}
        except (PolicyError, Exception) as e:
            err = sanitize_untrusted(f"{type(e).__name__}: {e}", 200)
            audit.record(iid, "action_failed", AGENT, {"error": err})
            return {"execution": {"status": "failed", "detail": {"error": err}}}

    def verify(state: IncidentState):
        iid, t, ex = state["incident_id"], state["target"], state.get("execution", {})
        action = state["proposal"]["action_id"]
        if ex.get("status") != "executed" or action not in ("rollout_undo", "restart_deployment"):
            reason = ("dry-run: nothing changed, nothing to verify" if ex.get("status") == "dry_run"
                      else "no cluster change was made; recovery after a merged PR must be verified separately (verify command)")
            res = {"status": "skipped", "checks": [], "reason": reason}
        else:
            res = wait_for_recovery(lambda: d.kube.collect(t["namespace"], t["name"], log_tail=5),
                                    s.verify_timeout_s, s.verify_interval_s, **({"sleep": d.sleep} if d.sleep else {}))
        audit.record(iid, "recovery_verification", AGENT, res)
        tm = elapsed(state)
        if res["status"] == "recovered" and "action_at" in tm:
            tm["action_to_recovered_s"] = res["waited_s"]
        return {"verification": res, "timings": tm}

    def record(state: IncidentState):
        iid = state["incident_id"]
        ex, ver, ap, pol = state.get("execution", {}), state.get("verification", {}), state.get("approval", {}), state.get("policy", {})
        if state.get("status") == "ignored":
            outcome = state["outcome"]
        elif ver.get("status") == "recovered":
            outcome = "action executed and recovery verified"
        elif ver.get("status") == "not_recovered":
            outcome = "action executed but recovery NOT verified"
        elif ex.get("status") == "dry_run":
            outcome = "dry-run only: no change made"
        elif ex.get("status") in ("failed", "blocked"):
            outcome = f"action {ex['status']}"
        elif ap.get("status") == "rejected":
            outcome = "remediation rejected by human"
        elif ex.get("status") == "executed":
            outcome = f"{state['proposal']['action_id']} executed (not a cluster change; verify after merge)"
        elif pol.get("decision") == "denied":
            outcome = "remediation denied by policy; diagnosis recorded"
        else:
            outcome = "diagnosis only (read-only); recommendation recorded"
        new = state | {"status": "closed" if state.get("status") != "ignored" else "ignored", "outcome": outcome}
        path = _write_outputs(new)
        audit.record(iid, "incident_closed", AGENT, {"outcome": outcome, "report": path})
        return {"status": new["status"], "outcome": outcome, "report_path": path}

    def _write_outputs(state: dict) -> str:
        base = Path(s.audit_dir)
        (base / "incidents").mkdir(parents=True, exist_ok=True)
        (base / "reports").mkdir(parents=True, exist_ok=True)
        from .redact import redact_obj
        safe = redact_obj({k: v for k, v in state.items()})
        (base / "incidents" / f"{state['incident_id']}.json").write_text(json.dumps(safe, indent=1, default=str))
        rp = base / "reports" / f"{state['incident_id']}.md"
        rp.write_text(render_report(safe, audit.events(state["incident_id"])))
        return str(rp)

    # ---------------------------------------------------------------- wiring
    g = StateGraph(IncidentState)
    for name, fn in [("identify", identify), ("collect_k8s", collect_k8s), ("collect_metrics", collect_metrics),
                     ("collect_logs", collect_logs), ("diagnose", diagnose), ("propose", propose),
                     ("evaluate_policy", evaluate_policy), ("notify_pending", notify_pending),
                     ("await_approval", await_approval), ("execute", execute), ("verify", verify),
                     ("record", record)]:
        g.add_node(name, fn)
    g.add_edge(START, "identify")
    g.add_conditional_edges("identify", lambda st: "record" if st.get("status") == "ignored" else "collect_k8s",
                            {"record": "record", "collect_k8s": "collect_k8s"})
    for a, b in [("collect_k8s", "collect_metrics"), ("collect_metrics", "collect_logs"), ("collect_logs", "diagnose"),
                 ("diagnose", "propose"), ("propose", "evaluate_policy"), ("notify_pending", "await_approval"),
                 ("execute", "verify"), ("verify", "record"), ("record", END)]:
        g.add_edge(a, b)
    g.add_conditional_edges("evaluate_policy", route_after_policy,
                            {"notify_pending": "notify_pending", "execute": "execute", "record": "record"})
    g.add_conditional_edges("await_approval", route_after_approval, {"execute": "execute", "record": "record"})
    return g.compile(checkpointer=checkpointer or MemorySaver())


def run_incident(graph, alert: dict, incident_id: str | None = None) -> dict:
    iid = incident_id or new_incident_id()
    cfg = {"configurable": {"thread_id": iid}}
    graph.invoke({"incident_id": iid, "created_at": iso(utcnow()), "alert": alert}, cfg)
    return graph.get_state(cfg).values | {"_pending": bool(graph.get_state(cfg).next)}


def resume_incident(graph, incident_id: str, decision: str, approver: str) -> dict:
    cfg = {"configurable": {"thread_id": incident_id}}
    if not graph.get_state(cfg).next:
        raise ValueError("incident is not awaiting approval")
    graph.invoke(Command(resume={"decision": decision, "approver": approver}), cfg)
    return graph.get_state(cfg).values
