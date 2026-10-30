# SkillPulse SRE/AIOps: demo checklist and interview explanation

Everything claimed here is something this repository does and measured on a local `kind` cluster. Where something is **not** done
it says so. Numbers are single runs, not statistics.

## 1. Thirty-second pitch

"I took a three-tier app (Go, Nginx, MySQL) with a GitHub Actions pipeline and built an incident-response layer around it on
Kubernetes. Prometheus, Loki and Grafana detect failures. A Grafana alert calls a LangGraph agent, which collects real evidence,
classifies the failure with deterministic rules, retrieves a runbook, proposes a rollback, and asks a human to approve it. A policy
file, not an LLM, decides what is allowed. I demonstrated one real approved rollback on a throw-away workload and proved the
audit trail. Argo CD (manual sync) and Sealed Secrets manage a sandbox namespace."

## 2. Pre-demo checklist (5 minutes)

- [ ] `kubectl config current-context` is `kind-skillpulse`; `kubectl get pods -A | grep -v Running` shows nothing unhealthy.
- [ ] Agent is in the safe resting state: `kubectl -n aiops get deploy aiops-agent -o jsonpath='{.spec.template.spec.containers[0].env}'`
      shows `AIOPS_MODE=approval`, `AIOPS_DRY_RUN=true`. `auto_remediation_enabled: false` in `aiops/policies/policy.yaml`.
- [ ] `aiops/scenarios/scenario.sh crashloop status`: `backend-sim` at the baseline revision, 1/1.
- [ ] `kubectl -n aiops exec deploy/aiops-agent -- python -m aiops.agent audit-verify` prints `audit chain: ok`.
- [ ] `make aiops-test` is green (`aiops/tests`, 366+ tests).
- [ ] Grafana reachable: `kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80`; the folder *AIOps* has 3 rules in *Normal*.
- [ ] Leave 10+ minutes between scenarios (Grafana grouping and the agent's 10-minute dedupe window).

## 3. Demo script (about 12 minutes)

1. **Architecture (2 min).** Show the diagram in `docs/aiops-architecture.md`. Say what is *not* there: no autonomous action, no LLM
   making decisions, no writes outside `backend-sim`.
2. **Cause a real failure (1 min).** `aiops/scenarios/scenario.sh crashloop observe`. It only applies a bad `PORT` to `backend-sim`
   and then watches; it never sends an alert.
3. **Detection (while it runs).** Show Grafana's rule go Pending, then Alerting; show the log line in Loki
   (`unknown port notaport`); show `backend-sim` pods: the old healthy pod keeps serving.
4. **The report (3 min).** `scripts/aiops-decide.sh list`, then `report <id>`. Point at: the alert came from Grafana; live
   Kubernetes evidence and the revision diff (`PORT 8080 -> notaport`); reproducible PromQL; diagnosis `crash_loop` /
   `invalid_configuration`; runbook; proposal `rollout_undo`; the rollback target's "was Ready: True" proof; policy
   `requires_approval`.
5. **The gate (2 min).** Show that nothing changed: `kubectl -n skillpulse-stg get deploy backend-sim` still broken. Try the wrong
   token or the webhook token: 401. Explain `--confirm` and that the token is never on a command line.
6. **Approve (1 min).** Only in Stage B mode (`approval-live`); otherwise approve the **dry run**: the audit log shows the same
   steps with "dry-run only: no change made".
7. **Audit (1 min).** `audit-verify`; show events: requested, approved by name, executed (revision 25 to 24), verified.
8. **Recover/verify manually** if you did not approve: `scenario.sh crashloop recover && scenario.sh crashloop verify`.
9. **GitOps (1 min).** `kubectl -n argocd get app skillpulse-sandbox`: Synced/Healthy, manual sync, no prune; the encrypted SealedSecret.

## 4. What to say about each component and why

| Piece | Why this and not something else |
|---|---|
| LangGraph | Explicit state machine with checkpoints: the approval step is an `interrupt()` that survives an agent restart (proved) |
| Deterministic policy | Safety must not depend on a model. A YAML rule book decides modes, allowlist, rate limit, confidence floor |
| Heuristic classifier + BM25 runbooks | Three failure families, cheap, explainable. I call it heuristic and uncalibrated, not "AI diagnosis" |
| Grafana alert rules, not Alertmanager | One place for rules and contact point; the rule's `receiver` goes straight to the agent webhook |
| Loki + Alloy | Logs as evidence; Alloy masks credentials before storage (a real bug was found: the first regex masked nothing) |
| Argo CD manual sync | Reviewable, reversible change path; no auto-sync, no prune, no adoption of existing workloads |
| Sealed Secrets (namespace-scoped) | Encrypted secrets in Git; the chart default ClusterRole could delete any Secret cluster-wide, so I removed it |
| Hash-chained audit log | Tamper-evident record of who approved what |

## 5. Likely interview questions and honest answers

**"Is this autonomous self-healing?"** No. Every real action needs a named human approval. Autonomous mode exists in code but is
disabled (`auto_remediation_enabled: false`, never configured). I demonstrated one approved rollback.

**"Did it improve MTTR?"** I did not measure that and don't claim it. I measured detection and execution timings of single runs on a
laptop cluster: alert to agent about 2 minutes (dominated by Grafana's evaluation and pending period, the agent itself takes 0.2 s),
approval to verified recovery 10 s.

**"What if the LLM hallucinates?"** The LLM is optional and off. Even if on, its output is validated against the closed action registry
and the policy; it cannot add an action or widen the allowlist.

**"What stops it deleting production?"** Four independent layers: policy (production allowlist empty, forbidden kinds), the closed
action registry (no delete/scale), RBAC (one Role: `get`+`patch` on `Deployment/backend-sim` in one namespace; verified with
`kubectl auth can-i`), and the approval gate. I tested the denials with real server-side dry-run patches as the agent identity.

**"What went wrong while building it?"** Real examples, all fixed: Alloy's masking regex replaced the wrong group; Helm 4 server-side
apply rejected a Grafana strategy change that `--dry-run=server` did not catch; I killed my own port-forward with an over-broad
`pkill`; a laptop sleep cut an observer run short; I found demo database credentials in Git history and rotated them with a
reversible `RETAIN CURRENT PASSWORD` procedure I rehearsed on a throw-away cluster; the backend image turned out to exist only in the
local node cache.

**"How would you take it to production?"** NetworkPolicy enforcement (kind has none), real secret management, pinned image digests,
Argo CD RBAC/SSO (its controller is cluster-admin equivalent), calibration of the classifier on real incident data, an on-call
integration, and a proper rate limit store (dedupe is in memory today). Terraform/AWS was deliberately postponed.

**"What are the limits?"** Three incident families; lexical retrieval; a fault shorter than about 2 minutes may not alert; single-run
timings; local cluster only.

## 6. Things I would not claim

Autonomous recovery, production MTTR improvement, production readiness, semantic RAG, calibrated confidence.
