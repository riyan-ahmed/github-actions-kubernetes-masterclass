# OOMKilled runbook

Applies to: a container terminated by the kernel because it exceeded its memory limit.
Pod status shows last state `Terminated`, reason `OOMKilled`, exit code 137; the pod often continues as CrashLoopBackOff.

## Symptoms
- `Last State: Terminated  Reason: OOMKilled  Exit Code: 137` in `kubectl describe pod`.
- Restart count rising; working-set memory in Prometheus close to the memory limit just before each restart.
- `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1`.

## Diagnosis
1. Confirm the reason: `kubectl get pod <pod> -n <ns> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}'`.
2. Compare the memory limit with usage: Prometheus `container_memory_working_set_bytes` against `kube_pod_container_resource_limits{resource="memory"}`.
3. Check whether the limit changed in the latest rollout: compare `resources.limits.memory` between the two newest ReplicaSets.
4. Distinguish a limit that is too low (usage plateaus at the limit immediately after start) from a leak (usage grows steadily over time).

## Root cause patterns
- **memory limit reduced**: the latest rollout lowered `resources.limits.memory` below what the process needs.
- **memory limit exceeded**: the limit never matched real usage, or load/data growth pushed usage past it.
- **memory leak**: working set climbs steadily across restarts; raising the limit only delays the kill.

## Safe remediation
1. If the limit was lowered by the latest rollout and the previous revision was healthy, roll back that rollout.
2. Otherwise raise the memory limit in the Git-managed Kustomize patch through a pull request (small step, for example double it, never beyond the policy cap) and let Argo CD sync after review.
3. Do not simply restart the pod: it will be killed again.
4. Never edit production Deployments directly; never touch the database volume.

## Verification
- No new `OOMKilled` terminations after 5 minutes; restart count is stable.
- Working-set memory stays comfortably below the limit.
- `kubectl rollout status deployment/<name> -n <ns>` succeeds and pods are Ready.
