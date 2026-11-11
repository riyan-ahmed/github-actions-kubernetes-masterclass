# SkillPulse AIOps: demo guide

A reproducible, narrated demo of the system on the local kind cluster. Every command and every number below comes
from real runs on 2026-10-28. Background: [architecture](aiops-architecture.md), [setup](aiops-setup.md).

**What you will show:** a real Kubernetes failure is caused on a throw-away workload; Prometheus and Grafana detect it
*by themselves*; Grafana delivers an authenticated webhook to a **read-only** LangGraph agent; the agent collects real
evidence, names the root cause, picks the right runbook, proposes a rollback, and records a tamper-evident audit trail;
you recover manually and verify.

**What you must not claim:** that the system "heals itself" (it does not touch the cluster), or that it improves MTTR
(these are single runs).

## 0. Before you start (1 minute)

```bash
kubectl config current-context                                   # kind-skillpulse
kubectl get pods -A | grep -v Running                            # nothing unhealthy
kubectl -n skillpulse-stg get deploy backend-sim                 # 1/1 (the throw-away fault target)
kubectl -n aiops get deploy aiops-agent -o jsonpath='{.spec.template.spec.containers[0].env[0].value}/{.spec.template.spec.containers[0].env[1].value}{"\n"}'   # diagnose/true
aiops/scenarios/scenario.sh crashloop status                     # baseline revision and current revision
```
Optional: open Grafana (`kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80`) → *Alerting → Alert rules*:
folder **AIOps** has three rules, all in state *Normal*.

Leave **at least 10 minutes** between scenarios: Grafana groups notifications (up to 5 minutes) and the agent merges
alerts for one workload for 10 minutes. `scenario.sh <name> observe` waits this out for you.

## 1. Demo A: CrashLoopBackOff, detected automatically (about 4 minutes)

```bash
aiops/scenarios/scenario.sh crashloop observe
```
This applies `PORT=notaport` to `backend-sim` and then **only watches** (GET requests; it never sends an alert).
Measured timeline (seconds after injection):

| Time | Event |
|---|---|
| 4.4 | Kubernetes: container exited with code 1 |
| 6.5 | Prometheus sees `CrashLoopBackOff` |
| 38.4 | Grafana rule *SkillPulse CrashLoopBackOff* → Pending |
| 98.8 | Grafana rule → **Alerting** |
| 127.4 | **The agent received Grafana's webhook** (30 s of that is Grafana batching) |
| 127.6 | Agent diagnosis complete (0.2 s) |
| 162.8 | Second rule (*Pod Running But Not Ready*) → Alerting; its notification is **deduplicated into the same incident** |

Show the report (the observer prints the incident id; the same text is available from the agent):

```bash
scripts/aiops-decide.sh list
scripts/aiops-decide.sh report <incident-id>
```
Point out, in this order:
1. **Alert**: `source: grafana-webhook`, labels set by Grafana (`pod`, `container`, `grafana_folder`), not typed by a human.
2. **Evidence** (all live): the old pod still `1/1 available` while the new pod is `CrashLoopBackOff`; the revision diff
   `backend.env.PORT: 8080 -> notaport`; the log line `listen tcp: lookup tcp/notaport: unknown port`; six reproducible
   PromQL queries.
3. **Diagnosis**: `crash_loop` / `invalid_configuration`, confidence 0.9 (a heuristic, not a probability).
4. **Runbook**: `crashloopbackoff.md` (ranked by BM25 from the evidence).
5. **Proposal and policy**: `rollout_undo`; the rollback target `backend-sim-f976846bf` had Ready pods in the last 24 h
   (**True**); policy `advisory_only` (*would be* `requires_approval`) because the agent is in diagnose mode.
6. **Audit trail**: nine events with hashes; **0 executed actions** in the whole log.

Manual investigation, then manual recovery:

```bash
aiops/scenarios/scenario.sh crashloop investigate          # the commands a human would run
aiops/scenarios/scenario.sh crashloop recover              # kubectl rollout undo to the last healthy revision
aiops/scenarios/scenario.sh crashloop verify               # spec == baseline, no failing container, agent's own recovery check
```
Measured: rollout complete **2.5 s** after the undo (the healthy v1 pod never stopped serving); Prometheus stopped
reporting the crash loop 30.9 s later; Grafana returned to *Normal* about 150 s after the fix (the `[2m]` smoothing in
the rule), and the *resolved* notification reached the agent and was ignored.

## 2. Demo B: OOMKilled (about 3.5 minutes)

```bash
aiops/scenarios/scenario.sh oom observe
aiops/scenarios/scenario.sh oom investigate
aiops/scenarios/scenario.sh oom recover && aiops/scenarios/scenario.sh oom verify
```
The fault lowers the memory limit to 6Mi. (An 8Mi limit was tried and does **not** kill the process; 3Mi is too small
for the container to start: the number was found by experiment.) Measured timeline:

| Time | Event |
|---|---|
| 2.3 | `lastState: Terminated, OOMKilled, exit 137` |
| 21.6 | Prometheus sees the signal; Grafana *CrashLoopBackOff* → Pending |
| 81.5 / 83.6 | Grafana *CrashLoopBackOff* / *OOMKilled* → Alerting |
| 113.3 | **Agent received the webhook** |

Teaching point: the incident was *opened* by the CrashLoopBackOff alert, yet the agent diagnosed `oom_killed` /
`memory_limit_reduced` (`backend.memory_limit: 128Mi -> 6Mi`) and retrieved `oomkilled.md`, because classification comes
from evidence, never from the alert name. Proposal: `rollout_undo`, advisory only.

## 3. Demo C: readiness failure (about 6 minutes)

```bash
aiops/scenarios/scenario.sh readiness observe
aiops/scenarios/scenario.sh readiness investigate
aiops/scenarios/scenario.sh readiness recover && aiops/scenarios/scenario.sh readiness verify
```
The fault points the readiness probe at `/healthz` (404). The pod stays `Running`, `0/1 Ready`, with **no restarts**, and
the Deployment keeps reporting 1 available replica. The rule has a 2-minute pending period, so this scenario is the
slowest. Measured: the pod is not Ready within 2.4 s, Prometheus has the signal at 30.3 s, the rule is
Pending at 64.5 s and **Alerting at 185.0 s**, and **the agent received the webhook at 214.7 s**. Diagnosis:
`readiness_failure` / `probe_misconfigured` (`backend.readiness_path: /health -> /healthz`, event
`Readiness probe failed: HTTP probe failed with statuscode: 404`), runbook `readiness-failure.md`, proposal `rollout_undo`
(advisory). Note: during this run a *resolved* notification from the previous scenario arrived; the agent correctly ignored it.

## 4. Demo D: the approval flow (Stage A and Stage B were both demonstrated)

The agent rests in `approval` mode with `dry_run=true` and one `get`+`patch` grant on `backend-sim`. A real rollback (Stage B)
was demonstrated once and then switched back off. The two stages are described in
[setup section 3](aiops-setup.md#3-controlled-remediation-stage-a-and-b-demonstrated-resting-state-dry-run):

1. `kubectl apply -k k8s/aiops/approval-dryrun` (adds one Role: `get`+`patch` on `backend-sim` only; mode `approval`,
   server-side **dry run**).
2. Run a scenario. The report now ends with an **Approval required** section containing the exact command.
3. `scripts/aiops-decide.sh approve <id> --approver <you> --confirm <id>`: the audit log records the approver, the policy
   re-check, the rollback-target proof (`was_ready: True`), the patch call (dry run) and the result. To run the full battery
   (invalid tokens, tamper attempt, unchanged-Deployment proof): `python3 aiops/scenarios/tools/approval_check.py <incident-id> approve`
   (or `reject`). Measured on 2026-10-28/29: 27 of 27 checks for approve and 13 of 13 for reject.
4. Stage B, the real rollback: `kubectl diff -k k8s/aiops/approval-live` (it must show only `AIOPS_DRY_RUN: "true" -> "false"`),
   `kubectl apply -k k8s/aiops/approval-live`, run the crash-loop scenario, approve **one** incident. Measured on 2026-10-30:
   Grafana alert to the agent in 119.3 s; the human approval was followed within the same second by one `rollout_undo`
   (revision 25 to 24) and `recovered: true` 10 s later. Then return to dry run: `kubectl apply -k k8s/aiops/approval-dryrun`.

Safety to point out: the token never appears on a command line, `--confirm` makes you retype the incident id, the policy
is re-evaluated at execution time, and `backend`, `frontend`, MySQL and production remain read-only.

## 5. Demo E: GitOps (sandbox installed and synced once)

Argo CD and Sealed Secrets are installed; the sandbox Application (`skillpulse-sandbox`, namespace `skillpulse-gitops`) was synced
by hand once: `argocd`-style manual sync created 8 objects in about 1 s and was Healthy in about 27 s, with a new MySQL on its
own volume and sandbox-only encrypted credentials. A read-only `kubectl diff -k k8s/gitops/stg` shows that a first sync of
staging would change **one annotation per object and nothing else** (no restarts); that step is not taken. See [setup section 4](aiops-setup.md#4-gitops-with-argo-cd-and-sealed-secrets--installed-for-the-sandbox-only)
for the sandbox-first migration plan.

## 6. After the demo

```bash
aiops/scenarios/scenario.sh <scenario> verify       # backend-sim healthy again
kubectl -n aiops exec -i deploy/aiops-agent -- python -m aiops.agent audit-verify     # chain ok
```
`backend-sim` can stay (it is idle and tiny). To remove it: `kubectl -n skillpulse-stg delete deployment backend-sim`.
Generated timelines and reports are written to `aiops/audit/scenarios/` (git-ignored).

## 6a. Measured results, all runs (seconds after injection; single runs, no statistics)

| Scenario | Run | Prometheus sees it | Grafana rule Alerting | Agent has the report | Diagnosis (confidence) | Policy decision |
|---|---|---|---|---|---|---|
| CrashLoopBackOff | 2026-10-28 | 6.5 | 98.8 | 127.6 | `crash_loop` / `invalid_configuration` (0.9) | advisory only (diagnose mode) |
| CrashLoopBackOff | 2026-10-30 (Stage B) | 8.8 | 87.2 | **119.3** | same (0.9) | `requires_approval`; approved; real rollback, recovered 10 s after approval |
| OOMKilled | 2026-10-28 | 21.6 | 81.5 / 83.6 | 113.3 | `oom_killed` / `memory_limit_reduced` (0.9) | advisory only |
| OOMKilled | 2026-10-30 | 12.1 | 73.6 / 75.7 | **103.3** | same (0.9), `backend.memory_limit: 128Mi -> 6Mi` | `requires_approval` (rejected by the test driver; recovered manually) |
| Readiness failure | 2026-10-28 | 30.3 | 185.0 | 214.7 | `readiness_failure` / `probe_misconfigured` (0.75) | advisory only |
| Readiness failure | 2026-10-30 | 29.9 | 186.4 | **216.0** | same (0.75), `readiness_path: /health -> /healthz`, HTTP 404 event | `requires_approval` (rejected by the test driver; recovered manually) |

Every row used the real alert path (Grafana evaluated the rule and called the webhook); the observer only watches. After each
scenario `scenario.sh <name> verify` passed (spec equals the healthy baseline, no failing container, the agent's own recovery check
`recovered = True`) and the audit chain stayed valid. The rollback target was proven Ready in the last 24 h in every report.
The 2026-10-30 runs were taken with the agent in `approval` mode; only the crash loop was approved and executed (Stage B).
Note: a laptop sleep delayed the start of the OOM run; the intervals above are measured within each run.

## 7. Talking points and honest numbers

* End-to-end, from fault to the agent holding a full report: **about 2 to 4 minutes**, dominated by monitoring
  intervals, not by the agent (0.2 s).
* One alert group per rule type: expect merged incidents when failures overlap.
* A fault shorter than about 2 minutes may never alert.
* The measurements are single runs: they describe this system, they do not prove an improvement over manual response.
