# CrashLoopBackOff runbook

Applies to: a container that starts, exits, and is restarted by the kubelet with growing back-off delays.
Kubernetes shows `CrashLoopBackOff` in the pod status, `Back-off restarting failed container` in events,
and `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}` in Prometheus.

## Symptoms
- Pod status `CrashLoopBackOff`, restart count increasing, `Ready 0/1`.
- Last state `Terminated` with reason `Error` and a non-zero exit code (1 for application errors, 137 is SIGKILL/OOM, 139 segfault).
- Deployment shows fewer available replicas than desired.

## Diagnosis
1. Read the logs of the previous container instance: `kubectl logs <pod> -n <ns> --previous`.
2. Check what changed: `kubectl rollout history deployment/<name> -n <ns>` and compare the last two ReplicaSets (image, env, command, args).
3. Look at the exit code and message: `kubectl describe pod <pod> -n <ns>`.
4. Typical causes: invalid environment variable such as a bad PORT (log shows `listen tcp: address ... unknown port`), unreachable database (`Could not connect to database`, `Waiting for database`), missing config, application panic after a bad image.

## Root cause patterns
- **invalid configuration**: process exits within seconds of start with a bind/listen error; a recent rollout changed an env var or argument.
- **database unreachable**: process retries then exits with `Could not connect to database`. Restarting the backend does not help; check MySQL.
- **application panic**: stack trace or `panic:` in logs, usually after an image change.

## Safe remediation
1. If the crash began with the latest rollout and the previous ReplicaSet was healthy, roll back: `kubectl rollout undo deployment/<name> -n <ns>`. In GitOps-managed environments revert the commit instead and let Argo CD sync.
2. For database problems, do not restart or roll back the backend; investigate the MySQL pod and its readiness.
3. Never delete PersistentVolumeClaims or the database to clear a crash loop.
4. Production changes always go through a reviewed pull request.

## Verification
- `kubectl get pods -n <ns>`: pods Running and `1/1` Ready with no new restarts after 2 minutes.
- `kubectl rollout status deployment/<name> -n <ns>` completes.
- `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}` returns no series.
