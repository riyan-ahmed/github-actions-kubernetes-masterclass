# Failure scenarios (staging only, `backend-sim` only)

Every scenario runs against the throwaway Deployment `backend-sim` in `skillpulse-stg`: same backend
image, no Service selecting it (no traffic), never `backend`, `frontend`, MySQL or anything in production.
Resource cost: 1 pod, 50m CPU / 32Mi requested. No disk is filled and no database data is touched.

## 1. CrashLoopBackOff (invalid PORT)

**Trigger**
```bash
kubectl apply -f aiops/scenarios/common/backend-sim-v1.yaml          # healthy baseline = revision 1; wait until Ready
kubectl apply -f aiops/scenarios/crashloop/backend-sim-v2-invalid-port.yaml   # revision 2: PORT=notaport
```
Wait about two minutes after the baseline so Prometheus has recorded the healthy pod as Ready: the agent
only recommends a rollback when the previous revision is *proven* healthy.

**Symptoms** (measured on 2026-10-28)
* new pod: `Error` exit 1 after ~2 s, `CrashLoopBackOff` after ~6 s, restarts growing with back-off;
* the old v1 pod keeps serving (`1/1 available`) because a rolling update never removes the last ready pod: the
  bad rollout causes no outage, it only blocks the rollout;
* Prometheus `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} == 1` after one scrape (~30 s).

**Manual investigation**
```bash
kubectl -n skillpulse-stg get pods -l app=backend-sim
kubectl -n skillpulse-stg describe pod <crashing-pod>                 # Last State: Terminated, Error, exit 1
kubectl -n skillpulse-stg logs <crashing-pod> --previous              # listen tcp: lookup tcp/notaport: unknown port
kubectl -n skillpulse-stg rollout history deployment/backend-sim
kubectl -n skillpulse-stg rollout history deployment/backend-sim --revision=2 | grep PORT   # PORT: notaport
```

**Expected root cause:** invalid configuration, the PORT environment variable introduced by the latest rollout.
Expected agent output: `crash_loop` / `invalid_configuration`, runbook `crashloopbackoff.md`, proposed
`rollout_undo`, policy `advisory_only` (would be `requires_approval`) in diagnose mode.

**Safe recovery** (manual; the agent has no write permission)
```bash
kubectl -n skillpulse-stg rollout undo deployment/backend-sim --to-revision=1
```
**Verify**
```bash
kubectl -n skillpulse-stg rollout status deployment/backend-sim
kubectl -n skillpulse-stg get pods -l app=backend-sim            # 1/1 Running, no CrashLoopBackOff
kubectl -n skillpulse-stg get deploy backend-sim -o jsonpath='{.spec.template.spec.containers[0].env[2].value}'   # 8080
kubectl -n aiops exec deploy/aiops-agent -- python -m aiops.agent verify --namespace skillpulse-stg --workload backend-sim
```

### Automatic detection: Grafana -> agent (measured 2026-10-28, one run, no manual POST)

Same fault, but nothing is sent by hand: Prometheus scrapes kube-state-metrics, Grafana evaluates the
`aiops-self-healing` rules and posts to the agent's authenticated webhook.

| Event | Seconds after injection |
|---|---|
| first crash visible in Kubernetes | 4.4 |
| Prometheus sees `CrashLoopBackOff` | 6.5 (depends on where the 30 s scrape lands) |
| Grafana rule `SkillPulse CrashLoopBackOff` Pending | 38.4 |
| Grafana rule Alerting (Pending 60 s) | 98.8 |
| agent received the Grafana webhook (alert `startsAt` to receipt: 30.3 s, Grafana group wait) | 127.4 |
| agent diagnosis complete (from receipt) | +0.2 s |
| second alert `Pod Running But Not Ready` Alerting / deduplicated into the same incident | 162.8 / 191 |

Manual recovery (`kubectl rollout undo deployment/backend-sim --to-revision=<last healthy revision>`) took 2.5 s
to a complete rollout; Prometheus stopped reporting the crash loop 30.9 s later; Grafana's smoothed
CrashLoop instance returned to Normal about 150 s after the fix (the `[2m]` smoothing); the *resolved*
notification is received by the agent and ignored. A crash loop shorter than about 2 minutes may not alert.
These are single-run measurements, not an MTTR claim.

**Remove the simulation:** `kubectl -n skillpulse-stg delete deployment backend-sim` (deletes only the throwaway workload).

## Driving all three scenarios: `scenario.sh`

```bash
aiops/scenarios/scenario.sh <crashloop|oom|readiness> status        # backend-sim and its revisions
aiops/scenarios/scenario.sh <scenario> observe       # inject, then watch Kubernetes/Prometheus/Grafana/agent (no manual alert)
aiops/scenarios/scenario.sh <scenario> inject        # just apply the fault (refuses unless backend-sim is a healthy baseline)
aiops/scenarios/scenario.sh <scenario> investigate   # the manual investigation commands, tailored (read-only)
aiops/scenarios/scenario.sh <scenario> recover       # kubectl rollout undo to the last healthy baseline revision
aiops/scenarios/scenario.sh <scenario> verify        # spec == baseline, no failing container, the agent's recovery check
```
Hard scope (enforced by tests): only `skillpulse-stg/backend-sim`, only manifests in this directory, refuses a wrong
kubectl context, `observe` performs GET requests only after the injection. Leave about 10 minutes between runs:
Grafana groups notifications for up to 5 minutes and the agent merges alerts for the same workload for 10 minutes
(`observe` waits this out and ignores *resolved* notifications from a previous run).

## 2. OOMKilled (memory limit too low)

**Trigger:** `scenario.sh oom inject` (memory request and limit `128Mi -> 6Mi`). The limit was found by experiment on this
cluster: **8Mi does not kill the process, 6Mi and 4Mi do (`OOMKilled`, exit 137), 3Mi cannot start (`StartError`).**

**Symptoms:** `lastState: Terminated, Reason: OOMKilled, Exit Code: 137`, then `CrashLoopBackOff`; the old pod keeps serving.

**Manual investigation:** `scenario.sh oom investigate` (or `kubectl describe pod`, the `lastState` of the container, and the
diff of `resources.limits.memory` between revisions).

**Expected root cause:** `oom_killed` / `memory_limit_reduced` (the latest rollout lowered the limit). Runbook `oomkilled.md`.
Proposed action `rollout_undo`; for a limit that was never right, `propose_config_pr` (bounded increase, through a PR).

**Recovery / verify:** `scenario.sh oom recover` then `verify`.

**Measured (one run, seconds after injection):** first `OOMKilled` 2.3; Prometheus signal 21.6; Grafana *CrashLoopBackOff* /
*OOMKilled* rules Alerting 81.5 / 83.6; **agent received the webhook 113.3**. The incident was opened by the CrashLoopBackOff
alert and still diagnosed as `oom_killed`: classification comes from evidence, not from the alert name.

## 3. Readiness failure (probe path does not exist)

**Trigger:** `scenario.sh readiness inject` (`readinessProbe.httpGet.path: /health -> /healthz`).

**Symptoms:** pod `Running`, `0/1 Ready`, **0 restarts** (liveness still passes), event
`Readiness probe failed: HTTP probe failed with statuscode: 404`; the Deployment still reports 1 available replica.

**Manual investigation:** `scenario.sh readiness investigate` (`kubectl describe pod`, events, the probe path in the revision diff).

**Expected root cause:** `readiness_failure` / `probe_misconfigured`. Runbook `readiness-failure.md`. Proposed action `rollout_undo`.
A 503 from the health endpoint would be classified `dependency_unavailable` and **escalated** (restarting the backend cannot fix MySQL).

**Measured (one run):** Pod-Not-Ready rule Pending 64.5; **Alerting 185.0** (2-minute pending period); **agent received the webhook 214.7**.
Classification confidence 0.75 (two evidence sources: Kubernetes and events; no restarts means no restart metric).

## Findings from running all three

* Per-pod rules are essential: in all three scenarios the Deployment kept **1 available replica**.
* Detection takes 2 to 4 minutes end to end; the agent itself needs 0.2 s.
* Back-to-back runs interact (notification grouping, agent dedupe window): space them out.
* A fault must outlive roughly 2 minutes to be sure of alerting.
* Single runs only: not an MTTR claim.
