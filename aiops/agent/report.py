"""Readable Markdown incident report rendered from the incident state and audit trail."""
from __future__ import annotations

import json


def _bullets(items) -> str:
    return "\n".join(f"- {i}" for i in items) or "- none"


def render_report(state: dict, audit_events: list[dict]) -> str:
    a, t = state.get("alert", {}), state.get("target", {})
    cls, diag = state.get("classification", {}), state.get("diagnosis", {})
    prop, pol = state.get("proposal", {}), state.get("policy", {})
    ap, ex, ver = state.get("approval", {}), state.get("execution", {}), state.get("verification", {})
    rb, ev = state.get("runbook") or {}, state.get("evidence", {})
    k8s, tm = ev.get("k8s", {}), state.get("timings", {})

    out = [f"# Incident {state['incident_id']}", "",
           f"- **Opened:** {state.get('created_at')}",
           f"- **Status:** {state.get('status')}  |  **Outcome:** {state.get('outcome', 'in progress')}",
           f"- **Alert:** {a.get('name')} ({a.get('status')}, source: {a.get('source')}, severity: {a.get('severity')})",
           f"- **Affected workload:** {t.get('kind')}/{t.get('name')} in namespace `{t.get('namespace')}`", ""]
    if a.get("injection_flags") or ev.get("logs", {}).get("injection_flags"):
        out += ["> **Warning:** instruction-like text was found in the alert or logs and was treated as data only.", ""]

    out += ["## Supporting evidence", "", "### Kubernetes"]
    d = k8s.get("deployment", {})
    if d:
        out.append(f"- Deployment `{d['name']}`: {d['replicas_available']}/{d['replicas_desired']} available, "
                   f"revision {d.get('current_revision')}")
    for p in k8s.get("pods", []):
        for c in p["containers"]:
            out.append(f"- Pod `{p['name']}` container `{c['name']}`: ready={c['ready']} restarts={c['restart_count']} "
                       f"state={c['state']} last_state={c['last_state']}")
    if k8s.get("revision_diff"):
        out += ["", "**Change between previous and current revision:**", _bullets(k8s["revision_diff"])]
    out += ["", "**Recent events:**", _bullets(f"{e['type']} {e['reason']} ({e['object']}): {e['message']}"
                                               for e in k8s.get("events", [])[:8])]
    out += ["", "### Prometheus (queries are reproducible)"]
    for name, m in ev.get("metrics", {}).items():
        if name == "previous_revision" and isinstance(m, dict):
            out.append(f"- `previous_revision` (rollback target `{m.get('replicaset')}`): had Ready pods in the last "
                       f"{m.get('window')}: **{m.get('was_ready')}**; query `{m.get('query')}`")
        elif isinstance(m, dict) and "query" in m:
            res = m.get("result", m.get("error"))
            out.append(f"- `{name}`: `{m['query']}` -> {json.dumps(res)[:300] if res != [] else 'no series'}")
    out += ["", "### Logs"]
    loki = ev.get("logs", {})
    if loki.get("lines"):
        out += [f"Source: Loki `{loki.get('query')}`", "```", *[l["line"] for l in loki["lines"][:15]], "```"]
    else:
        prev = [l for ls in k8s.get("previous_logs", {}).values() for l in ls[-12:]]
        out += ["Source: Kubernetes API (previous container logs)" if prev else "No log evidence available.",
                "```", *prev, "```"]
    if ev.get("errors"):
        out += ["", "**Collection problems (evidence is partial):**", _bullets(ev["errors"])]

    out += ["", "## Diagnosis", "",
            f"- **Incident type:** {cls.get('incident_type')}  |  **Suspected root cause:** `{cls.get('root_cause')}`",
            f"- **Confidence (heuristic, uncalibrated):** {cls.get('confidence')}",
            f"- **Explanation ({diag.get('source')}):** {diag.get('explanation')}"]
    if diag.get("llm_error"):
        out.append(f"- LLM unavailable, fell back to rules: {diag['llm_error']}")
    if diag.get("disagreement"):
        out.append(f"- **Rules and LLM disagree:** rules={cls.get('incident_type')} llm={diag.get('llm_incident_type')}")
    out += ["", "Signals:", _bullets(f"[{s['source']}] {s['signal']}" for s in cls.get("signals", []))]
    out += ["", "## Runbook", "",
            (f"- **Retrieved:** {rb['title']} (`{rb['doc']}`, BM25 score {rb['score']})" if rb
             else "- **Retrieved:** none (incident not classified, so no runbook is suggested)")]
    for s in rb.get("sections", [])[:2]:
        out += ["", f"> **{s['heading']}** (score {s['score']})", *[f"> {l}" for l in s["text"].splitlines()[:6]]]

    out += ["", "## Remediation and policy", "",
            f"- **Proposed action:** `{prop.get('action_id')}` {json.dumps(prop.get('params'))}",
            f"- **Rationale:** {prop.get('rationale')}",
            f"- **Policy decision:** `{pol.get('decision')}`" + (f" (would be `{pol['would_be']}` if the mode allowed execution)" if pol.get('would_be') else ""),
            f"- **Policy reasons:** {'; '.join(pol.get('reasons', []))}",
            f"- **Approval status:** {ap.get('status', 'n/a')}" + (f" by {ap.get('approver')}" if ap.get('approver') else ""),
            f"- **Action taken:** {ex.get('status', 'none')} {json.dumps(ex.get('detail')) if ex.get('detail') else ''}",
            f"- **Verification:** {ver.get('status', 'not performed')}"]
    out += ["", _bullets(f"{'PASS' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}" for c in ver.get("checks", []))]
    if ap.get("status") == "pending":
        dry = pol.get("dry_run")
        out += ["", "## Approval required", "",
                f"A human must decide before anything happens. If approved the agent will run `{prop.get('action_id')}` on "
                f"`{t.get('namespace')}/{t.get('name')}`"
                + (" as a **server-side dry run (nothing will change)**." if dry else " **for real** and then verify recovery."),
                "", "```bash",
                f"scripts/aiops-decide.sh approve {state['incident_id']} --approver <your-name> --confirm {state['incident_id']}",
                f"scripts/aiops-decide.sh reject  {state['incident_id']} --approver <your-name> --confirm {state['incident_id']}",
                "```", "",
                "The decision, the approver and the result are written to the audit log."]

    out += ["", "## Measured timings (raw measurements, no MTTR claim)", ""]
    out += [_bullets(f"{k}: {v}" for k, v in tm.items())]
    out += ["", "## Audit trail", "", "| time | event | actor |", "|---|---|---|"]
    out += [f"| {e['ts']} | {e['event']} | {e['actor']} |" for e in audit_events]
    return "\n".join(out) + "\n"
