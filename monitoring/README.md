# Monitoring additions for the AIOps agent: install, verify, roll back

Everything here is **proposed, not applied**. Each numbered step changes the live `kind-skillpulse`
cluster and needs separate approval. Steps marked *(read-only)* change nothing.

Pinned versions (verified with `helm show chart`):

| Component | Chart | Version | App |
|---|---|---|---|
| Loki | `oci://ghcr.io/grafana-community/helm-charts/loki` (the old `grafana/loki` repo entry is gone) | 20.0.1 | 3.7.9, `Monolithic` mode (`-target=all`, one StatefulSet replica) |
| Alloy | `grafana/alloy` | 1.14.3 | v1.21.2 |
| Existing stack | `prometheus-community/kube-prometheus-stack`, release `monitoring` | 92.13.1 (installed) | Grafana 13.2.4 |

Files: `loki-values.yaml`, `alloy-values.yaml`, `aiops-grafana-values.yaml`,
`grafana-persistence-values.yaml` (optional, recommended), existing `values.yaml` (unchanged).

## What gets added

| Object | Namespace | CPU req | Mem req | Mem limit | Storage |
|---|---|---|---|---|---|
| `loki-0` (StatefulSet, 1 container, no sidecar) | monitoring | 50m | 200Mi | 400Mi | PVC `storage-loki-0` 2Gi (`standard`, local-path) |
| `alloy-*` (Deployment, 2 containers: alloy + config-reloader) | monitoring | 60m | 146Mi | 256Mi (reloader unlimited) | none |
| Grafana pod is **recreated** (same 3 containers) | monitoring | unchanged (none set) | unchanged | unchanged | PVC `grafana-data` 1Gi (created separately) |
| aiops agent (later phase) | aiops | ~50m | ~128Mi | 256Mi | PVC 100Mi |

Total new requests, excluding the agent: about 110m CPU and 346Mi memory. The three kind nodes show
3-13 % of their memory requested today (Docker VM: 12 CPUs, 7.75 GiB), so there is ample headroom.
New pods: 2 (Loki, Alloy). New PVCs: 1 (Loki) or 2 (with Grafana persistence).

## RBAC added

| Who | Scope | Permissions |
|---|---|---|
| Loki | none | No Role, no ClusterRole (rules sidecar disabled; the chart default would have been cluster-wide get/list/watch on all Secrets and ConfigMaps) |
| Alloy | 4 namespaced Roles: `skillpulse`, `-dev`, `-stg`, `-prd` | `pods`: get, list, watch. `pods/log`: get. **No** Secrets, ConfigMaps, nodes, CRDs, ClusterRole. (Chart default was a ClusterRole including Secrets/ConfigMaps/nodes/metrics.) |
| Grafana | unchanged | Reads Secret `aiops-webhook` only through the pod env (`secretKeyRef`) |

`monitoring/validate/loki_alloy_local_check.py` fails if a rendered Role or ClusterRole ever mentions
secrets, configmaps, `nodes/metrics` or `*`, or if Alloy's Roles leave those four namespaces.

## Grafana persistence: design

Problem: Grafana's `storage` volume is an `emptyDir` and there is no PVC. Any pod recreation
(a Helm upgrade, a node drain, an eviction) deletes every alert rule, contact point, policy,
dashboard, silence, library panel and user created in the UI.

Design (`grafana-persistence-values.yaml` + `grafana-pvc.yaml`):

1. A 1Gi PVC `grafana-data` is created **outside Helm** and only referenced
   (`persistence.existingClaim`). Helm does not own it, so `helm rollback`, `helm upgrade` and
   `helm uninstall` can never delete it. (A Helm-owned PVC would be deleted by a rollback to
   revision 1, which has none.)
2. Strategy `RollingUpdate` with `maxSurge: 0` / `maxUnavailable: 1` (the old pod is taken down first) and
   `initChownData` off (local-path creates 0777 directories; Grafana runs as uid 472). See the next section for
   why this is not `Recreate`.
3. The upgrade that introduces the volume still recreates the pod once, so UI-created objects are lost
   exactly one last time: **backup before, restore after.** Later restarts and upgrades are safe.
4. The StorageClass `standard` has `reclaimPolicy: Delete`: deleting the PVC deletes the data. Optional
   extra protection after the PVC is bound: `kubectl patch pv <name> -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'`.
5. Long term, treat the PVC as a convenience: define important things as code (the aiops rules already are).
   On kind the PVC lives on a node disk; deleting the cluster deletes it.

Backup/restore coverage (verified against real Grafana 13.2.4 by `validate/grafana_backup_restore_check.py`
and, for the real backup, `validate/grafana_restore_rehearsal.py`): alert rules, rule-group intervals,
contact points, notification policy tree, mute timings, templates, folders, dashboards (UI-created only;
sidecar-provisioned ones return by themselves), datasources, library panels, active silences, org
preferences. Not restorable: datasource/contact-point secrets (Grafana never exports them: re-enter) and
users/teams/service accounts (inventory only). Annotations and dashboard history are not backed up.

## Known risk: SQLite pod overlap on future Grafana upgrades

Why not `type: Recreate`: Helm 4 applies manifests with server-side apply. The live Grafana Deployment
carried the API-server-defaulted `rollingUpdate: {maxSurge: 25%, maxUnavailable: 25%}`, and switching to
`Recreate` made the first upgrade attempt (Helm revision 2, 2026-10-28) fail with
`spec.strategy.rollingUpdate: Forbidden: may not be specified when strategy type is 'Recreate'`.
`helm upgrade --dry-run=server` did NOT catch this; a direct
`kubectl apply --server-side --dry-run=server --field-manager=helm -f <rendered Deployment>` reproduced it
and also showed that `RollingUpdate` with `maxSurge: 0, maxUnavailable: 1` is accepted. Always run that
direct dry run before changing a strategy.

The residual risk of `maxSurge: 0` / `maxUnavailable: 1`:
* The Deployment controller does not count a Terminating pod, so the replacement pod can start while the old
  pod is still shutting down (up to `terminationGracePeriodSeconds`). Both can then open the same SQLite
  database on `grafana-data`. A real `Recreate` waits until the old pod is gone.
* On a real cluster a ReadWriteOnce volume usually blocks the second attach; kind's local-path volumes do not.
* Grafana normally retries on a locked SQLite database, so damage is unlikely but not impossible.
* The first upgrade (emptyDir to PVC) had no overlap risk: the old pod wrote to its emptyDir, the new pod got
  the empty PVC.

Mitigations: back up before every Grafana upgrade (`scripts/grafana-alerting-backup.sh`), keep alert rules and
datasources as code where possible, and evaluate a permanent true `Recreate` separately. The clean way to get
there is a one-time targeted patch that removes the defaulted field
(`kubectl -n monitoring patch deployment monitoring-grafana --type=merge -p '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}'`;
no pod restart) followed by a values change, tested with the direct server-side dry run first.

## Alert rules: what is detected and how fast

| Rule | Signal (per pod) | Pending | Notes |
|---|---|---|---|
| SkillPulse CrashLoopBackOff | `max_over_time(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}[2m])` | 1 m | per-POD on purpose |
| SkillPulse OOMKilled | last terminated reason OOMKilled and restarts increased in 10 m | 0 s | |
| SkillPulse Pod Running But Not Ready | `kube_pod_status_ready{condition="false"}` and phase Running | 2 m | |

Why per pod: in the measured backend-sim failure the Deployment kept **1 available replica the whole
time** (the old pod keeps serving during a bad rollout), so deployment-level rules, including the existing
"Backend Replicas Unavailable", never fire. Evaluated on the real Prometheus history of that run, the raw
crash-loop series (visible for 90 s) fired in 6 of 12 Grafana scheduler phases; the `[2m]`-smoothed
expression fires in 12 of 12. Expect alert latency of roughly: scrape (30 s) + evaluation (up to 60 s) +
pending (60 s), i.e. about 1.5 to 2.5 minutes after a crash loop starts, and a fault shorter than that may
not alert. The `[2m]` smoothing also delays "resolved" by about two minutes.

## Offline validation (no cluster needed)

```bash
make aiops-test                     # 204 unit tests
make aiops-validate-monitoring      # needs helm + docker: real Grafana, Loki and Alloy in containers
aiops/.venv/bin/python monitoring/validate/grafana_backup_restore_check.py
```

## Installation order

Dependency order: storage-free pieces first, Grafana last (it restarts and needs the webhook Secret
and, ideally, a running agent to receive alerts).

### 0. Preflight (read-only)

```bash
kubectl config current-context                       # must be kind-skillpulse
kubectl get nodes; kubectl get pods -A | grep -v Running   # cluster healthy
kubectl get sc standard                              # local-path, WaitForFirstConsumer
helm list -A                                         # monitoring rev 1 deployed
helm repo update
helm get values monitoring -n monitoring -o yaml > aiops/audit/helm-values-monitoring-rev1.yaml   # git-ignored
```

### 1. Loki (approval needed: creates a StatefulSet + 2Gi PVC)

```bash
helm install loki oci://ghcr.io/grafana-community/helm-charts/loki --version 20.0.1 \
  -n monitoring -f monitoring/loki-values.yaml
# verify
kubectl -n monitoring rollout status statefulset/loki --timeout=180s
kubectl -n monitoring get pod loki-0 pvc/storage-loki-0
kubectl -n monitoring port-forward svc/loki 3100:3100 &      # then:
curl -s localhost:3100/ready                                 # -> ready
kill %1
```
Rollback: `helm uninstall loki -n monitoring && kubectl -n monitoring delete pvc storage-loki-0`
(a StatefulSet's PVC is not removed by Helm).

### 2. Alloy and its namespaced Roles (approval needed: 4 Roles + 4 RoleBindings in the SkillPulse namespaces)

```bash
helm install alloy grafana/alloy --version 1.14.3 -n monitoring -f monitoring/alloy-values.yaml
# verify
kubectl -n monitoring rollout status deployment/alloy --timeout=120s
kubectl -n monitoring logs deploy/alloy -c alloy --tail=30        # no "forbidden" errors
SA=system:serviceaccount:monitoring:alloy
kubectl auth can-i get pods/log  --as=$SA -n skillpulse-stg       # yes
kubectl auth can-i list pods     --as=$SA -n skillpulse-prd       # yes
kubectl auth can-i get secrets   --as=$SA -n skillpulse-stg       # no
kubectl auth can-i list pods     --as=$SA -n kube-system          # no
kubectl auth can-i list pods     --as=$SA --all-namespaces        # no
# logs reach Loki
kubectl -n monitoring port-forward svc/loki 3100:3100 &
curl -sG localhost:3100/loki/api/v1/query_range --data-urlencode 'query={namespace="skillpulse-stg", app="backend"}' --data-urlencode 'limit=5' | head -c 600
kill %1
```
Rollback: `helm uninstall alloy -n monitoring` (removes the Roles too). Known risk: the minimal
Roles could not be exercised offline. If Alloy logs `forbidden`, uninstall and report the exact
verb/resource instead of widening the Role blindly.

### 3. Webhook Secret (approval needed: creates Secrets)

The token must exist before the Grafana upgrade, otherwise the Grafana pod fails with
`CreateContainerConfigError`.

```bash
TOKEN=$(openssl rand -hex 24)
kubectl -n monitoring create secret generic aiops-webhook --from-literal=token="$TOKEN"
# later phase: the agent reads the same value from the aiops namespace
kubectl create namespace aiops
kubectl -n aiops create secret generic aiops-webhook --from-literal=token="$TOKEN"
kubectl -n aiops create secret generic aiops-approval --from-literal=token="$(openssl rand -hex 24)"
unset TOKEN
```
Rollback: `kubectl -n monitoring delete secret aiops-webhook; kubectl delete namespace aiops`.

### 4. Back up Grafana (approval needed to read Secret `monitoring-grafana` and port-forward; read-only on Grafana)

```bash
scripts/grafana-alerting-backup.sh        # -> aiops/audit/grafana-backup-<ts>/ (git-ignored, mode 700)
ls -la aiops/audit/grafana-backup-*/ && cat aiops/audit/grafana-backup-*/alert-rules.json | head -c 400
```
Stop here if the counts printed do not match what you see in the Grafana UI.

### 5. Agent first (v0.4.0 proposed; v0.3.0 is what runs today), so the webhook has a receiver (approval needed)

```bash
kind load docker-image skillpulse-aiops-agent:0.4.0 --name skillpulse     # 0.2.0 stays on the nodes for rollback
kubectl diff -k aiops/                      # only the Deployment image line may differ
kubectl apply -k aiops/ && kubectl -n aiops rollout status deployment/aiops-agent --timeout=150s
# rollback: kubectl -n aiops rollout undo deployment/aiops-agent   (audit PVC untouched)
```
The agent stays `AIOPS_MODE=diagnose`, `AIOPS_DRY_RUN=true`, read-only RBAC.

### 6. Refresh the backup, create the Grafana PVC, upgrade Grafana (approval needed)

```bash
scripts/grafana-alerting-backup.sh                     # refresh; verify counts; stop if they do not match the UI
kubectl apply -f monitoring/grafana-pvc.yaml           # Pending until the pod mounts it (WaitForFirstConsumer)
helm upgrade monitoring prometheus-community/kube-prometheus-stack --version 92.13.1 -n monitoring \
  -f monitoring/values.yaml -f monitoring/aiops-grafana-values.yaml -f monitoring/grafana-persistence-values.yaml
kubectl -n monitoring rollout status deployment/monitoring-grafana --timeout=240s
kubectl -n monitoring get pvc grafana-data             # Bound
```
Expected Helm changes (verified with `helm template` diffs and `helm upgrade --dry-run=server`): exactly four
objects: ConfigMap `monitoring-grafana` (alerting files), ConfigMap `...-grafana-datasource` (+Loki),
Deployment `monitoring-grafana` (env from Secret, PVC volume, Recreate), and Secret `monitoring-grafana`
(unchanged in reality: the dry run proved the admin password is preserved). The release's pre-upgrade hook
Jobs (admission certgen) run again with temporary RBAC and are cleaned up by Helm; if a hook fails the upgrade
stops before Grafana changes. Grafana is unavailable for roughly 30 to 60 seconds (Recreate).

### 7. Restore the UI configuration and verify

```bash
scripts/grafana-alerting-restore.sh aiops/audit/grafana-backup-<ts>
# Grafana (port-forward svc/monitoring-grafana 3000:80):
#  Alerting > Alert rules: your 2 SkillPulse rules (folder SkillPulse) AND folder AIOps with 3 rules (file-provisioned)
#  Alerting > Contact points: aiops-agent (+ the built-in empty)   Notification policies: unchanged
#  Connections > Data sources: Prometheus, Loki (Explore: {namespace="skillpulse-stg", app="backend-sim"})
```

### 8. Success test: a real failure, detected by Grafana, delivered to the agent (approval needed)

No manual POST. `backend-sim` is already healthy at revision 3.
```bash
kubectl apply -f aiops/scenarios/crashloop/backend-sim-v2-invalid-port.yaml      # inject; hold the fault >= 5 minutes
# watch (read-only): Grafana alert state, then the agent's incident list (token from the Secret via a curl config)
kubectl -n skillpulse-stg get pods -l app=backend-sim -w
# Grafana API: GET /api/prometheus/grafana/api/v1/rules  -> state pending, then firing
# agent: GET /incidents  and  GET /incidents/<id>/report  (source: grafana-webhook, NOT cli)
kubectl -n skillpulse-stg rollout undo deployment/backend-sim --to-revision=1    # manual recovery
```

### Rollback of the Grafana upgrade (protects the PVC)

```bash
scripts/grafana-alerting-backup.sh                     # capture anything created since the upgrade
helm rollback monitoring 1 -n monitoring               # Grafana returns to emptyDir; grafana-data is NOT owned by Helm and survives
kubectl -n monitoring rollout status deployment/monitoring-grafana
scripts/grafana-alerting-restore.sh aiops/audit/grafana-backup-<ts>
kubectl -n monitoring get pvc grafana-data             # still Bound: re-upgrade later reuses the data
```
Deleting `grafana-data` is a separate, deliberate `kubectl delete pvc` and is never part of a rollback.

## Limitations of the offline validation

* Real Grafana, Loki and Alloy ran in Docker, but Kubernetes discovery (`discovery.kubernetes`,
  `loki.source.kubernetes`) and the minimal Alloy Roles can only be proven on the live cluster.
* The Grafana test fed rules from a mock Prometheus, so it proves provisioning, routing and the
  Bearer header, not real SkillPulse alert conditions.
* Only the OOMKilled alert had been delivered when the first check finished (the other two rules
  have `for: 1m` / `for: 2m`); all three rules were provisioned and evaluating.
