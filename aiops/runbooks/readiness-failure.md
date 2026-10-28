# Readiness failure runbook

Applies to: pods that are Running but never become Ready, so the Service has no endpoints and the Deployment has fewer available replicas than desired.
Events show `Readiness probe failed`; `kube_pod_status_ready{condition="true"}` is 0.

## Symptoms
- `kubectl get pods` shows `Running` with `0/1` Ready and no restarts (liveness is passing).
- Event: `Readiness probe failed: HTTP probe failed with statuscode: 404` (wrong path) or `connection refused` (not listening) or `statuscode: 503` (unhealthy dependency).
- The Service endpoints list is empty; requests to the backend fail through the frontend.

## Diagnosis
1. Read probe failures: `kubectl describe pod <pod> -n <ns>` and look at the Events section.
2. Compare the probe definition with what the application serves: the SkillPulse backend answers on `/health` port 8080 and returns 503 when MySQL is unreachable.
3. Check what changed in the latest rollout: probe path, port, image, environment.
4. If the application returns 503 or `unhealthy`, check the dependency: `kubectl get pods -n <ns> -l app=mysql` and MySQL readiness.

## Root cause patterns
- **probe misconfigured**: HTTP 404 on the probe path after a rollout changed the path; the application log shows the 404 for the probe request.
- **app not listening**: connection refused; port or bind address wrong.
- **dependency unavailable**: health endpoint returns 503 because the database is down; restarting the backend does not help.

## Safe remediation
1. For a misconfigured probe introduced by the latest rollout, roll back to the previous revision (`kubectl rollout undo`), or revert the commit in GitOps-managed environments.
2. For a dependency failure, fix the dependency (MySQL) and leave the backend alone; do not restart or scale the database storage.
3. Do not weaken or remove the readiness probe to make the symptom disappear.

## Verification
- `kubectl get pods -n <ns>`: `1/1` Ready.
- `kubectl get endpoints <service> -n <ns>` lists pod addresses.
- `kubectl rollout status deployment/<name> -n <ns>` succeeds; `kube_deployment_status_replicas_available` equals desired replicas.
