# GitOps-driven recovery of an OOMKilled workload

This is the one place in the project where the agent's remediation **leaves the cluster entirely**. The agent diagnoses an
`OOMKilled` failure and opens a **GitHub pull request**; a **human merges it**; **Argo CD** applies the merge. The agent
never patches, rolls back, restarts or syncs anything. It then *observes* the recovery and writes the whole timeline to its
hash-chained audit log.

Related: [architecture](aiops-architecture.md) · [setup](aiops-setup.md) · [demo](aiops-demo.md).

## 1. What is built

```mermaid
flowchart LR
  subgraph git["GitHub (branch gitops-demo)"]
    MAN["k8s/gitops/oom-demo<br/>deployment.yaml + patches/resources.yaml"]
    PR["Pull request<br/>aiops/inc-... -> gitops-demo"]
  end
  subgraph cluster["kind-skillpulse"]
    subgraph sbx["skillpulse-gitops (sandbox namespace)"]
      DEMO["oom-demo Deployment<br/>(isolated test workload)"]
    end
    KSM[kube-state-metrics] --> PROM[(Prometheus)]
    DEMO --> KSM
    DEMO -->|logs| LOKI[(Loki)]
    PROM --> GRAF["Grafana alert rule<br/>aiops-oomkilled-gitops-demo"]
    GRAF -->|"1. webhook (Bearer)"| AGENT
    subgraph ag["aiops: LangGraph agent"]
      AGENT["ingest -> evidence -> diagnose<br/>-> propose -> POLICY"]
      TRK["gitops tracker<br/>(observe only)"]
      AUD[(audit log<br/>hash chain)]
    end
    AGENT -.->|"2. read-only: events, PromQL, LogQL"| DEMO
    ARGO["Argo CD app oom-demo<br/>auto-sync, prune=false"] -->|"6. sync"| DEMO
  end
  AGENT -->|"3. open PR (1 file, limits.memory only)"| PR
  HUMAN([human reviewer]) -->|"4. review + MERGE"| PR
  PR -->|merge| MAN
  MAN -->|"5. Argo CD polls Git"| ARGO
  TRK -.->|"7. observe PR, Argo app, workload"| AUD
```

| Piece | Where | Role |
|---|---|---|
| `oom-demo` workload | `aiops/demo/oom-demo/` (image), `k8s/gitops/oom-demo/` (manifests) | Synthetic service whose memory demand is `ALLOC_MB`. 12 MiB is healthy under a 32Mi limit; 40 MiB is OOMKilled. Not part of SkillPulse. |
| Argo CD `Application oom-demo` | `gitops/argocd/oom-demo/application.yaml` | **Automatic sync, `prune: false`**, no `selfHeal`, no finalizer, own `AppProject` (Deployment only, sandbox namespace only). The existing manual-sync estate is untouched. |
| Grafana rule `aiops-oomkilled-gitops-demo` | `monitoring/aiops-grafana-values.yaml` | Own group evaluated every 10 s; routes to the existing `aiops-agent` contact point. |
| Policy | `aiops/policies/policy.yaml` | `skillpulse-gitops`: empty write allowlist, only `propose_config_pr` / `escalate_human`, memory cap 512Mi, one allow-listed target file. |
| PR module | `aiops/agent/gitops.py` | Creates a branch, writes **one** file, opens the PR. Path must be under `k8s/gitops/`. No merge call exists. |
| Tracker | `aiops/agent/gitops_verify.py` | After the PR: waits for the merge, the Argo CD sync of *that* commit, a healthy workload running the *proposed* limit; records each step. |
| Overlay | `k8s/aiops/approval-gitops-pr/` | Turns on the PR path. Cluster writes stay in server-side dry run; mode stays `approval`; autonomy stays off. |
| Two independent switches | `AIOPS_DRY_RUN` (Kubernetes writes) and `AIOPS_PR_DRY_RUN` (GitHub PRs) | A real PR needs `AIOPS_GITHUB_ENABLED=true` **and** `AIOPS_PR_DRY_RUN=false`. Setting it grants no Kubernetes permission: RBAC is read-only and the policy denies `rollout_undo`/`restart_deployment` in `skillpulse-gitops` regardless. Both default to dry run. |

## 2. Automatic diagnosis, human approval, GitOps recovery: three different things

| | Who/what does it | Evidence it happened | Can it change the cluster? |
|---|---|---|---|
| **Automatic diagnosis** | The agent, within seconds of the alert: reads Kubernetes events and pod state, runs PromQL, reads Loki, classifies deterministically (`oom_killed` / `memory_limit_exceeded`), retrieves the runbook, proposes `propose_config_pr` with limit x2 capped at 512Mi, and the **policy file** (not an LLM) permits a PR. | `diagnosis`, `action_proposed`, `policy_decision` audit events | **No.** Read-only RBAC. |
| **Human approval** | A person reviews the PR (root cause, evidence, one-file diff) and **merges it**. The agent has no merge permission and no code path that merges. | GitHub `merged_by` / `merged_at`, audit event `pr_merged` | Not by itself: a merge changes Git only. |
| **GitOps recovery** | **Argo CD** notices the new commit on `gitops-demo`, renders the Kustomize patch and applies the Deployment. Kubernetes rolls the pod. | `argocd_synced` (with the merge commit SHA) then `workload_recovered` (limit applied + pod Ready) | **Yes, but only Argo CD** (its own identity), only the Deployment, pruning disabled. |

Opening the PR needs no *pre*-approval because it changes nothing in the cluster; the merge is the approval. That is the
`pr_allowed` policy decision, enabled only for `skillpulse-gitops` through `pr_without_preapproval`. Staging and production
still require explicit approval for `propose_config_pr`.

## 3. Guarantees, and how each is tested

| Requirement | Mechanism | Test |
|---|---|---|
| Agent cannot write the GitOps workload | Policy `write_allowlist: []`, `rollout_undo`/`restart_deployment` not allowed there; RBAC grants only `get/list/watch` | `test_every_cluster_write_is_denied_for_the_gitops_workload`, `test_agent_rbac_has_no_write_permission_on_the_gitops_namespace_or_argocd`, live `kubectl auth can-i` in `gitops-oom-demo.sh preflight` |
| PR touches only the approved manifest | `gitops_targets` pins one path; `check_pr_path` rejects traversal/absolute/out-of-prefix; one `PUT` per PR; limits-only patch | `test_pr_changes_exactly_one_file_and_only_limits_memory`, `test_pr_path_outside_scope_is_refused`, `test_a_policy_target_outside_the_scope_cannot_produce_a_pr` |
| Real PR while Kubernetes stays dry-run | `AIOPS_PR_DRY_RUN=false`, `AIOPS_DRY_RUN=true` | `test_pr_dry_run_is_its_own_switch_and_ignores_the_kubernetes_dry_run`, `test_live_pr_with_kubernetes_dry_run_on_opens_a_real_pr_and_never_touches_the_cluster`, `test_direct_kubernetes_writes_stay_blocked_even_when_prs_are_live`, `test_kubernetes_writes_keep_their_own_dry_run_flag` |
| Agent never merges | No merge API call in the module | `test_the_agent_never_merges_or_pushes_to_the_base_branch` |
| Memory cap, confidence floor | `validate_action_request`, `min_confidence_for_execution` | `test_memory_cap_is_enforced`, `test_low_confidence_gets_no_pr` |
| Recovery is *caused by the merge* | Tracker requires Argo CD `Synced`/`Succeeded` at the **merge SHA** and the live memory limit equal to the proposal | `test_a_sync_of_an_older_revision_does_not_count`, `test_recovery_requires_the_proposed_limit_to_be_live` |
| Argo CD cannot prune or cascade | `prune: false`, no finalizer, `Prune=false,Delete=false` on the object, Deployment-only project | `test_oom_demo_application_has_automatic_sync_and_no_pruning`, `test_oom_demo_project_is_isolated` |
| Demo script never changes the cluster directly | No write `kubectl` verb in `scripts/gitops-oom-demo.sh` | `test_demo_scripts_never_use_kubectl_to_change_the_workload` |
| Audit trail tamper-evident | SHA-256 hash chain, secrets redacted | `test_full_gitops_recovery_is_recorded_in_order` (+ existing audit tests) |

## 4. Going live (every step below changes something outside the repo and needs explicit approval)

Order matters. Nothing here touches `skillpulse`, `skillpulse-dev`, `-stg`, `-prd`, the existing `skillpulse-sandbox` app, or any PVC.

0. (Already done) **Protect `gitops-demo`:** `.github/workflows/gitops-demo-ci.yml` must exist on that branch, then require a pull request and the status check `validate-oom-demo`, including for administrators. Direct pushes are rejected for the owner and the agent token (`GH006`), so `scripts/gitops-oom-demo.sh inject|reset` now open CI-checked PRs and merge them (no `--admin`).
1. **Publish the branches.** Push `feature/gitops-oom-remediation`; create branch `gitops-demo` from it (Argo CD tracks it and the agent's PRs target it, so `main` is never touched).
2. **GitHub token.** Fine-grained personal access token, repository `github-actions-kubernetes-masterclass` only, permissions *Contents: read/write* and *Pull requests: read/write*, short expiry. Store it only in a cluster Secret:
   `kubectl --context kind-skillpulse -n aiops create secret generic aiops-github --from-literal=token=<token>`
3. **Load images** into the kind nodes: `kind load docker-image skillpulse-oom-demo:1.0 skillpulse-aiops-agent:0.5.0 --name skillpulse`.
4. **Argo CD:** `kubectl apply -f gitops/argocd/oom-demo/project.yaml -f gitops/argocd/oom-demo/application.yaml`.
5. **Grafana alert rule:** `helm upgrade` of the `monitoring` release with `-f monitoring/aiops-grafana-values.yaml` (see `docs/aiops-setup.md` for the backup step first), or create the same rule through the Grafana API.
6. **Agent:** `kubectl diff -k k8s/aiops/approval-gitops-pr`, review, then `kubectl apply -k k8s/aiops/approval-gitops-pr`. New privileges are *read-only*: `get/list/watch` pods, events, deployments in `skillpulse-gitops`, and `get` on the single Argo CD Application `oom-demo`.
7. `scripts/gitops-oom-demo.sh preflight` must end with `RESULT: the agent has no write permission ...`.

Rollback of everything: delete the Application (no finalizer, so the Deployment is not cascaded), re-apply `k8s/aiops/approval-dryrun`.

## 5. Running one test

```bash
scripts/gitops-oom-demo.sh preflight          # read-only: Argo app Synced/Healthy, agent config, agent has no write RBAC
scripts/gitops-oom-demo.sh inject             # CI-checked PR + merge: ALLOC_MB 12 -> 40 on gitops-demo; Argo CD syncs it; the pod is OOMKilled
# Grafana fires -> webhook -> agent -> PR appears on GitHub (watch: scripts/gitops-oom-demo.sh status)
# a human reviews the PR in GitHub and merges it
scripts/gitops-oom-demo.sh measure <incident-id>   # stage durations; the incident id is in the PR body / agent logs
scripts/gitops-oom-demo.sh reset              # CI-checked PR + merge back to the healthy baseline
# or the whole loop with checks and timings:  aiops/.venv/bin/python aiops/scenarios/tools/gitops_trials.py --human   (you merge the PR)
```

`python -m aiops.agent timeline <incident-id>` and `GET /incidents/<id>/timeline` give the same stages from the audit log alone.

### What is measured, and what is not

| Stage | From -> to | Source |
|---|---|---|
| injection to OOM | Git push -> first `OOMKilled` | push time recorded by the script; `kube_pod_container_status_last_terminated_timestamp` |
| detection | first `OOMKilled` -> alert received by the agent | same metric; audit `alert_received` |
| diagnosis | alert received -> `diagnosis` event | audit log |
| PR creation | `diagnosis` -> PR `created_at` | audit log; GitHub |
| human approval | PR created -> `merged_at` | GitHub (**a person, not automated**) |
| Argo CD pickup | merge -> sync operation started | Application `status.operationState` |
| Argo CD sync | started -> finished | same |
| rollout | sync finished -> new pod `Ready` | pod `Ready` condition (the Deployment itself never goes unavailable: the old pod keeps serving while the new one crash-loops) |
| total | first OOM -> new pod Ready (with and without the merge step) | derived |

Argo CD discovers Git changes by polling (this cluster: `timeout.reconciliation` 180 s), so "merge to sync" is
dominated by that interval unless a webhook or a refresh is configured. No webhook is configured, because a `kind` cluster is not
reachable from GitHub.

## 6. Measured results

Three official trials ran back to back on 2026-11-11 (`aiops/scenarios/tools/gitops_trials.py`), all on the isolated `oom-demo`
workload and the `gitops-demo` branch. **All three completed the full loop with every gate passing:**
real OOMKilled (exit 137) -> Grafana alert -> LangGraph diagnosis (`oom_killed`, confidence 0.9) -> real GitHub PR (one file,
one line) -> merge -> Argo CD automatic sync of that merge commit -> new pod Ready on the 64Mi limit -> Argo CD `Synced`/`Healthy`
-> audit chain verified. Nothing was patched, rolled back or synced by hand; the workload was reset between runs by a Git commit.

> **Read this before quoting any number.**
> * **The merge in trials 1-3 was not a human review** (the separate run below was). The repository owner delegated the merge to the harness, which merged
>   only after checking, in code: base branch `gitops-demo`, exactly one file, exactly `memory: 32Mi -> 64Mi`, one commit, mergeable,
>   no failing checks (none are configured on this branch). "PR created -> merge" below (6-9 s) is script time, **not** human
>   think time. The design still puts a person at that point; these trials demonstrate everything around it.
> * **No run recovered in under 90 s** (fastest: 182 s) and no such claim is made.
> * Sources have 1 s resolution (GitHub, Argo CD, Kubernetes); the agent's audit clock is finer. "Sync duration 0.0" means under 1 s.

| Stage (s) | Run 1 | Run 2 | Run 3 | median |
|---|---|---|---|---|
| injection_to_first_oom | 148.1 | 179.6 | 61.5 | 148.1 |
| first_oom_to_agent_alert | 44.1 | 56.0 | 331.0 | 56.0 |
| alert_to_diagnosis | 0.3 | 0.2 | 0.2 | 0.2 |
| diagnosis_to_pr_created | 2.7 | 2.8 | 3.8 | 2.8 |
| pr_created_to_merge | 6.0 | 9.0 | 9.0 | 9.0 |
| merge_to_argocd_sync_started | 127.0 | 265.0 | 188.0 | 188.0 |
| argocd_sync_duration | 0.0 | 0.0 | 0.0 | 0.0 |
| sync_to_pod_ready | 2.0 | 5.0 | 3.0 | 3.0 |
| first_oom_to_pr_created | 47.1 | 59.0 | 335.0 | 59.0 |
| total_first_oom_to_healthy_incl_merge_gate | 182.0 | 338.0 | 535.0 | 338.0 |
| total_excl_merge_gate | 176.0 | 329.0 | 526.0 | 329.0 |
| total_excl_merge_gate_and_argocd_poll | 49.0 | 64.0 | 338.0 | 64.0 |

| Run | Incident | PR | Inject commit | PR head | Merge commit | Argo CD revision (Synced/Healthy) | Reset commit | Audit chain |
|---|---|---|---|---|---|---|---|---|
| 1 | `inc-20261111-100011-db64e8` | [#3](https://github.com/riyan-ahmed/github-actions-kubernetes-masterclass/pull/3) | `927ff3311f` | `dd42805c21` | `1c12549d49` | `1c12549d49` | `9fd3a108ba` | ok |
| 2 | `inc-20261111-101210-160875` | [#4](https://github.com/riyan-ahmed/github-actions-kubernetes-masterclass/pull/4) | `3f1ed910e1` | `2681e39381` | `9286ed0efe` | `9286ed0efe` | `7c5adba7d4` | ok |
| 3 | `inc-20261111-103209-49d5f0` | [#5](https://github.com/riyan-ahmed/github-actions-kubernetes-masterclass/pull/5) | `42c8f1c7be` | `db63ede350` | `370f3c313f` | `370f3c313f` | `dd226feb10` | ok |

| Run | Pod OOMKilled | Diagnosis | Policy decision | Files changed in PR | Diff |
|---|---|---|---|---|---|
| 1 | `oom-demo-74bf4c4bc5-txbbj` exit 137 at 2026-11-11T09:59:27Z | oom_killed / memory_limit_exceeded, confidence 0.9 | pr_allowed (propose_config_pr) | 1 | `-              memory: 32Mi` -> `+              memory: 64Mi` |
| 2 | `oom-demo-74bf4c4bc5-b7hn9` exit 137 at 2026-11-11T10:11:14Z | oom_killed / memory_limit_exceeded, confidence 0.9 | pr_allowed (propose_config_pr) | 1 | `-              memory: 32Mi` -> `+              memory: 64Mi` |
| 3 | `oom-demo-74bf4c4bc5-jdbzz` exit 137 at 2026-11-11T10:26:39Z | oom_killed / memory_limit_exceeded, confidence 0.9 | pr_allowed (propose_config_pr) | 1 | `-              memory: 32Mi` -> `+              memory: 64Mi` |

`first_oom` is the first `OOMKilled` termination read from Kubernetes by the harness. Stage definitions are in section 5.

### What the numbers say

| | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Agent work: alert received -> PR created | 3.0 s | 3.0 s | 4.0 s |
| First OOM -> PR created (detection + agent) | 47.1 s | 59.0 s | 335.0 s |
| Argo CD pickup after merge (Git poll) | 127 s | 265 s | 188 s |
| **First OOM -> pod Ready, incl. merge step** | **182 s** | **338 s** | **535 s** |
| Same, excluding the merge step and Argo CD's Git poll | 49 s | 64 s | 338 s |

* **The agent is fast; the platform timers are slow.** Diagnosis takes 0.2-0.3 s and PR creation about 3 s. The recovery time is
  dominated by two timers the agent does not control:
  * **Argo CD's Git polling** (127-265 s here, default 180 s interval, no webhook on a local `kind` cluster). This was left at its
    default on purpose; it was not tuned or bypassed.
  * **Grafana notification timing.** In Run 3, Grafana's own state history shows the rule went `Alerting` at 10:27:40 (61 s after the
    OOM, in line with Runs 1-2) but the webhook only reached the agent at 10:32:09, 269 s later; in Runs 1-2 delivery took about
    1 s. This is consistent with Grafana's notification-group `group_interval` (default 5 min) applying to a group that had already
    notified, but the policy settings were not inspected, so it is a likely cause, not a proven one. A shorter `group_interval` in the
    rule's `notification_settings` is the obvious next experiment.
* **Variance is real.** Three runs are enough to show the loop works repeatably, not enough for a statistical claim. The medians are
  descriptive only.
* **Alert-to-OOM and injection timings** (60-180 s from injection to the first OOM) also depend on the same Argo CD poll, because
  the injection itself is deployed by Argo CD.

### One human-reviewed run (2026-11-11, after CI and branch protection were added)

The three trials above used a scripted merge. To exercise the real control point, one more run stopped at the agent's PR and
the repository owner reviewed and merged it in the GitHub UI ("Create a merge commit", no admin override). By then `gitops-demo`
was protected (pull request + required check `validate-oom-demo`, enforced for administrators), so the harness's own failure
injection and reset were CI-checked PRs too ([#6](https://github.com/riyan-ahmed/github-actions-kubernetes-masterclass/pull/6) and its reset).

| Stage | Seconds |
|---|---|
| Injection merged -> first OOMKilled | 144.0 |
| First OOM -> alert at the agent | 58.1 |
| Alert -> diagnosis | 0.3 |
| Diagnosis -> PR created | 2.7 |
| **PR created -> merged by the human reviewer** | **558.0** (9 min 18 s of real review) |
| Merge -> Argo CD sync started (Git poll) | 260.0 |
| Argo CD sync duration | under 1 |
| Sync finished -> new pod Ready | 5.0 |
| **First OOM -> pod Ready, including the human review** | **884.0** |
| Same, excluding the human review | 326.0 |
| Same, excluding the human review and Argo CD's Git poll | 66.0 |

PR [#7](https://github.com/riyan-ahmed/github-actions-kubernetes-masterclass/pull/7): incident `inc-20261111-110911-ecbe77`,
pod `oom-demo-74bf4c4bc5-gj7wb` OOMKilled (exit 137) at 11:08:13Z, diagnosis `oom_killed / memory_limit_exceeded` (0.9), policy
`pr_allowed`, one file and one line (`32Mi -> 64Mi`), required check passed on the head commit `39fd7bb939`, merged by `riyan-ahmed`
as `b543aad6e2`, Argo CD `Synced`/`Healthy` at the same revision, baseline restored (`5120afae22`), audit chain `ok`.
Raw record: `docs/evidence/gitops-oom/run-human.json`. This is a single run; it shows the human step works, not a trend.

### How the trials were run, and what went wrong first

The evidence directory `docs/evidence/gitops-oom/` holds the three run records (`run-N.json`: raw timestamps, PR and commit SHAs,
Argo CD revisions, policy and diagnosis, audit-chain result), the official log, and the logs of four earlier attempts that did **not**
count:

1. **Attempt 0** stopped at the PR-diff gate: the agent re-serialised the whole patch file, so the text diff contained re-indented
   lines beside the memory change. PR #1 was closed unmerged. Fix: the agent now edits only the `limits.memory` line and refuses to
   open a PR if the file is not the expected shape (agent 0.5.2).
2. **Attempt 2** recovered correctly through Git and Argo CD (PR #2) but the harness stopped on a race: the old pod was still
   `Terminating`. Fix: terminating pods are ignored. PR #2 is merged but is not an official trial.
3. **Attempts 1 and 3** were discarded because the laptop slept (the second time on battery), freezing the Docker VM and every timer.
   The harness now compares wall-clock and monotonic time and aborts if the machine slept.
4. After the runs, the measurement tool was found to take "failure onset" from a Prometheus metric that holds the **latest**
   termination of a crash-looping container, which under-reported detection time (for example 10 s instead of 44 s in Run 1). The
   tool now takes the first OOM from the harness, and the tables above are recomputed from the raw timestamps by
   `gitops_report.py`. The `measurements` field stored inside `run-N.json` predates this fix; use the report script's figures.

### Evidence per run

Each PR is a real, public pull request in this repository (links in the table above); the agent opened it, the harness merged it.

## 7. Limits

* Local `kind` cluster, one synthetic workload, one failure class. This demonstrates the mechanism, not production readiness.
* The agent's PR proposes "double the limit, capped at 512Mi". It cannot distinguish a leak from an undersized limit; the PR text says so and the runbook covers it.
* Trials 1-3 ran before `gitops-demo` had CI or branch protection, so "checks passing" was vacuous there and the merge gate relied on the content checks. The human-reviewed run was protected (PR + `validate-oom-demo`, enforced for admins). Required approving reviews are 0, because the agent's PRs are opened under the same GitHub identity as the reviewer, and GitHub does not let you approve your own PR.
* The tracker lives in the agent process. If the pod restarts mid-incident, resume with `python -m aiops.agent track <incident-id>`.
* The GitHub token is a bearer credential held in a cluster Secret. It can open PRs and create branches in this one repository; it cannot merge past a branch-protection rule if one is added to `gitops-demo`.
