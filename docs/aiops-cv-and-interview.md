# CV bullets and interview answers: GitOps self-healing

Every claim below is backed by the repository (tests, manifests, evidence). Wording is deliberately precise about what was automated
and what was not. **Do not** write "autonomous self-healing", "human-approved" for the trial merges, or any recovery time under
180 s: the three measured recoveries were 182 s, 338 s and 535 s from the first OOMKilled to a Ready pod.

## One-line description
Built a GitOps-driven incident-response agent on Kubernetes: Grafana alerts a LangGraph agent, which diagnoses an OOMKilled workload
from Kubernetes, Prometheus and Loki evidence and opens a one-line GitHub pull request; after merge, Argo CD applies the fix.
The agent has no write access to the workload.

## CV bullets by role

**DevOps Engineer**
* Closed the loop from alert to fix through Git: Grafana webhook -> LangGraph agent -> GitHub PR -> merge -> Argo CD auto-sync
  (prune disabled), demonstrated end to end three times on kind with recoveries of 182 s, 338 s and 535 s (first OOM to Ready pod).
* Enforced a reviewable change: the agent edits a single `limits.memory` line (32Mi -> 64Mi) in one pinned file, fails closed if the
  file shape is unexpected, and is capped at 512Mi by a policy file.
* Wrote a trial harness that injects failure through Git, validates each PR (branch, file, exact diff) before merging, measures every
  stage from raw timestamps and detects laptop sleep so invalid runs abort.

**Platform Engineer**
* Designed a self-service-safe remediation interface: the platform's only automated output is a PR against an allow-listed manifest;
  deployment stays with Argo CD under a dedicated AppProject (Deployment only, one namespace, no cluster-scoped resources).
* Separated "may open a PR" (`AIOPS_PR_DRY_RUN`) from "may touch the cluster" (`AIOPS_DRY_RUN`, read-only RBAC, empty write allowlist),
  proven by 5 targeted tests and a live `kubectl auth can-i` preflight before every run.
* Added a read-only tracker that confirms recovery was caused by the merge: Argo CD synced the merge commit SHA and the live limit
  equals the proposal, recorded in a hash-chained audit log.

**Cloud Engineer**
* Operated a multi-component Kubernetes observability and GitOps stack (Prometheus, Grafana, Loki, Argo CD, Sealed Secrets) and added a
  version-controlled Grafana alert rule rolled out through a Helm upgrade after an alert/dashboard backup and a manifest diff.
* Diagnosed platform-level latency from measurements: Argo CD Git polling (127-265 s) and a 269 s Grafana notification delay in one run
  dominated recovery time while the agent's own work was about 3 s. (Local kind cluster; no cloud provider was used.)

**Site Reliability Engineer**
* Measured recovery per stage across three controlled OOMKilled incidents (first OOM -> alert 44-331 s, alert -> PR 3-4 s, merge ->
  Argo CD sync 127-265 s) and reported the variance and unexplained outlier instead of an average; made no sub-90-second claim.
* Built deterministic diagnosis (rules + runbook retrieval, confidence 0.9) with a policy engine, confidence floor, rate limits and
  circuit-breaker style gates; LLM output cannot widen what is allowed.
* Found and fixed real defects through the trials: a PR that re-indented unrelated lines, a harness race on terminating pods,
  a measurement bug that under-reported detection time, and sleep-corrupted runs.

**DevSecOps Engineer**
* Least-privilege design verified rather than assumed: read-only Roles, a single `get` on one Argo CD Application, no merge capability,
  a fine-grained token limited to one repository, and a live check that every write verb on the workload returns "no".
* Containerised the agent on a digest-pinned Chainguard base (non-root, read-only filesystem); refreshed digests and `oauthlib` to clear
  new Trivy HIGH findings; image gate with 34 checks passes.
* Treated alert content as untrusted input (sanitisation, injection flags), kept secrets out of Git (token in a cluster Secret, never
  logged), and used a tamper-evident audit log that verified in-pod after every run.

## Interview answers

**Walk me through the flow.** Prometheus sees the OOMKilled container; a Grafana rule fires and posts to the agent with a bearer
token. The agent collects Kubernetes events and pod state, PromQL metrics and Loki logs, classifies deterministically, retrieves the
runbook, proposes "raise the limit", and the policy file allows only a PR. The PR changes one line. Someone merges it; Argo CD syncs
that commit; a tracker confirms the pod is Ready on the new limit and writes the timeline to the audit log.

**Where is the human?** At the merge. Automatic diagnosis, human approval and GitOps recovery are three separate steps with three
different identities (agent, reviewer, Argo CD). In my trials I delegated the merge to a harness that checked the diff in code, so
those runs did not exercise a human reviewer; I say so in the write-up.

**Why not just `kubectl patch` or `rollout undo`?** That bypasses review and Git history and creates drift that Argo CD would revert.
The agent has no write permission on the workload, so a bug or a prompt injection cannot patch it.

**Why was recovery slow?** Because of timers outside the agent: Argo CD polls Git every 180 s by default, and in one run Grafana held
the notification for 269 s. The agent's own work was about 3 s. Webhooks or a shorter `group_interval` would help; I left the
defaults untouched so the numbers are honest.

**What went wrong during the trials?** A PR diff gate caught the agent reformatting a whole file (fixed to a one-line edit that
fails closed); my harness raced a terminating pod; my laptop slept and corrupted two runs; and my first measurement logic used the
latest OOM instead of the first. All are in `docs/evidence/gitops-oom/`.

**Limits.** One synthetic workload on a local cluster, three runs, no CI on the demo branch, and the agent cannot tell a leak from an
undersized limit (the PR says so).
