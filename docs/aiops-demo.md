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

## 4. Demo D: the approval flow (Stage A, dry run, is applied)

The agent currently runs in `approval` mode with `dry_run=true` and one `get`+`patch` grant on `backend-sim`. A real rollback
(Stage B) is not applied. The two stages are described in
[setup section 3](aiops-setup.md#3-controlled-remediation-stage-a-applied-stage-b-not-applied):

1. `kubectl apply -k k8s/aiops/approval-dryrun` (adds one Role: `get`+`patch` on `backend-sim` only; mode `approval`,
   server-side **dry run**).
2. Run a scenario. The report now ends with an **Approval required** section containing the exact command.
3. `scripts/aiops-decide.sh approve <id> --approver <you> --confirm <id>`: the audit log records the approver, the policy
   re-check, the rollback-target proof (`was_ready: True`), the patch call (dry run) and the result. To run the full battery
   (invalid tokens, tamper attempt, unchanged-Deployment proof): `python3 aiops/scenarios/tools/approval_check.py <incident-id> approve`
   (or `reject`). Measured on 2026-10-28/29: 27 of 27 checks for approve and 13 of 13 for reject.
4. Only if you want to see a real rollback: `kubectl apply -k k8s/aiops/approval-live`, repeat; the agent rolls `backend-sim`
   back and verifies recovery (two healthy polls in a row).

Safety to point out: the token never appears on a command line, `--confirm` makes you retype the incident id, the policy
is re-evaluated at execution time, and `backend`, `frontend`, MySQL and production remain read-only.

## 5. Demo E: GitOps (optional, not installed)

`k8s/gitops/*` and `gitops/argocd/*` are written and validated offline. A read-only
`kubectl diff -k k8s/gitops/stg` shows that a first Argo CD sync would change **one annotation per object and nothing
else** (no restarts). See [setup section 4](aiops-setup.md#4-gitops-with-argo-cd-and-sealed-secrets--not-yet-applied)
for the sandbox-first migration plan.

## 6. After the demo

```bash
aiops/scenarios/scenario.sh <scenario> verify       # backend-sim healthy again
kubectl -n aiops exec -i deploy/aiops-agent -- python -m aiops.agent audit-verify     # chain ok
```
`backend-sim` can stay (it is idle and tiny). To remove it: `kubectl -n skillpulse-stg delete deployment backend-sim`.
Generated timelines and reports are written to `aiops/audit/scenarios/` (git-ignored).

## 7. Talking points and honest numbers

* End-to-end, from fault to the agent holding a full report: **about 2 to 4 minutes**, dominated by monitoring
  intervals, not by the agent (0.2 s).
* One alert group per rule type: expect merged incidents when failures overlap.
* A fault shorter than about 2 minutes may never alert.
* The measurements are single runs: they describe this system, they do not prove an improvement over manual response.
