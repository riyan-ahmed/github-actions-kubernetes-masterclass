# SkillPulse AIOps: setup guide

How to build, test and deploy the system from a clean checkout on the local kind cluster, in the order the
pieces depend on each other. Every command below was run for real while building this project, except where a
section says otherwise. Architecture: [aiops-architecture.md](aiops-architecture.md).

Conventions: run everything from the repository root. `kubectl` must point at the `kind-skillpulse` context.
Anything that changes the cluster says so; read it before you run it.

## 0. Prerequisites

| Tool | Used for | Checked version |
|---|---|---|
| Docker Desktop (arm64 on Apple silicon) | building the agent image, offline validation containers | Docker host `aarch64` |
| kind, kubectl, helm | the cluster and the charts | kind 0.x with node image v1.35.1; helm v4 |
| Python 3.12+ | agent tests (a venv is used) | 3.14 locally, 3.15 in the image |
| trivy, shellcheck, yamllint | security and lint checks | optional but used by the checks below |

The cluster already has `monitoring/kube-prometheus-stack` (release `monitoring`, Alertmanager disabled) and the
namespaces `skillpulse-stg`, `skillpulse-prd` with their workloads. The Helm repos used:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update
```

Note: the Loki chart now lives in an OCI registry (`oci://ghcr.io/grafana-community/helm-charts/loki`).

## 1. Local development and tests (no cluster needed)

```bash
make aiops-venv            # python3 -m venv aiops/.venv + dependencies
make aiops-test            # the full unit and integration suite (359 tests at the time of writing)
make aiops-validate-monitoring   # real Grafana, Loki and Alloy in Docker with the Helm-rendered config (about 10 minutes)
```

`aiops-validate-monitoring` starts throwaway containers only; it never touches the cluster. It checks provisioning,
the Bearer header, Loki/Alloy credential masking, backup and restore of 13 Grafana object types, and a real
Grafana delivering to the real agent image.

The agent image (see [`aiops/docker/README.md`](../aiops/docker/README.md) for why this base image):

```bash
make aiops-image           # docker build --platform linux/arm64 -t skillpulse-aiops-agent:0.4.0
make aiops-image-check     # Trivy gate (no ignore flags) + 34 container checks as uid 10001
```

## 2. Install order on the cluster

Each step lists what it changes, how to verify, and how to roll back. All were applied in this order.

### 2.1 Loki  (StatefulSet + 2Gi PVC)
```bash
helm install loki oci://ghcr.io/grafana-community/helm-charts/loki --version 20.0.1 -n monitoring -f monitoring/loki-values.yaml
kubectl -n monitoring rollout status statefulset/loki --timeout=180s
kubectl -n monitoring port-forward svc/loki 3100:3100 &  curl -s localhost:3100/ready      # -> ready
```
Rollback: `helm uninstall loki -n monitoring && kubectl -n monitoring delete pvc storage-loki-0`.
The values disable the chart's rules sidecar, whose ClusterRole could read every Secret and ConfigMap.

### 2.2 Alloy  (Deployment + 4 namespaced Roles)
```bash
helm install alloy grafana/alloy --version 1.14.3 -n monitoring -f monitoring/alloy-values.yaml
SA=system:serviceaccount:monitoring:alloy
kubectl auth can-i get pods/log --as=$SA -n skillpulse-stg      # yes
kubectl auth can-i get secrets  --as=$SA -n skillpulse-stg      # no
```
The values replace the chart's default ClusterRole (cluster-wide Secret/ConfigMap read) with pods + pod logs in
the four SkillPulse namespaces only, and mask credentials before logs are stored.

### 2.3 Namespace and authentication Secrets
```bash
kubectl create namespace aiops && kubectl label namespace aiops app.kubernetes.io/part-of=skillpulse-aiops
W=$(openssl rand -hex 32)
printf %s "$W" | kubectl -n monitoring create secret generic aiops-webhook --from-file=token=/dev/stdin
printf %s "$W" | kubectl -n aiops      create secret generic aiops-webhook --from-file=token=/dev/stdin
unset W
printf %s "$(openssl rand -hex 32)" | kubectl -n aiops create secret generic aiops-approval --from-file=token=/dev/stdin
```
Two different tokens: the webhook token (Grafana to agent, also read access to incidents) and the approval token
(only a human approving a remediation). Values travel through pipes only: never in arguments, files or output. Use
`printf %s` (no trailing newline).

### 2.4 The agent (read-only, diagnose, dry-run)
```bash
kind load docker-image skillpulse-aiops-agent:0.4.0 --name skillpulse
kubectl diff -k aiops/                       # review
kubectl apply -k aiops/
kubectl -n aiops rollout status deployment/aiops-agent --timeout=150s
```
Verify: `/healthz` shows `mode=diagnose dry_run=true`; wrong or missing tokens return 401; the service account has no
write permission (`kubectl auth can-i patch deployments.apps -n skillpulse-stg --as=system:serviceaccount:aiops:aiops-agent`
is `no`). Roll back with `kubectl -n aiops rollout undo deployment/aiops-agent` (the audit PVC is untouched).

### 2.5 Grafana: back up, create the data volume, upgrade, restore
```bash
scripts/grafana-alerting-backup.sh                     # read-only; writes aiops/audit/grafana-backup-<ts>/ (700, git-ignored)
kubectl apply -f monitoring/grafana-pvc.yaml           # PVC grafana-data; stays Pending until the pod mounts it
# BEFORE the upgrade: test the Deployment with the direct dry run (helm's own --dry-run=server does NOT catch strategy errors)
helm template monitoring prometheus-community/kube-prometheus-stack --version 92.13.1 -n monitoring \
  -f monitoring/values.yaml -f monitoring/aiops-grafana-values.yaml -f monitoring/grafana-persistence-values.yaml > /tmp/render.yaml
#   extract the monitoring-grafana Deployment and run:
#   kubectl apply --server-side --dry-run=server --field-manager=helm -f <that Deployment file>
helm upgrade monitoring prometheus-community/kube-prometheus-stack --version 92.13.1 -n monitoring \
  -f monitoring/values.yaml -f monitoring/aiops-grafana-values.yaml -f monitoring/grafana-persistence-values.yaml --timeout 8m
kubectl -n monitoring rollout status deployment/monitoring-grafana --timeout=240s
scripts/grafana-alerting-restore.sh aiops/audit/grafana-backup-<ts>
```
What to expect: the Grafana pod is replaced in about 10 seconds; `grafana-data` becomes Bound; UI-created rules are
gone until the restore; five rules exist afterwards (3 AIOps, file-provisioned; your 2, restored and editable).
**Read [`monitoring/README.md`](../monitoring/README.md)** for the persistence design, the failed first attempt
(`Recreate` rejected by server-side apply) and the SQLite pod-overlap risk of the `maxSurge: 0` strategy.

Rollback: back up again, `helm rollback monitoring 1 -n monitoring` (the PVC survives because Helm does not own it),
then restore. Deleting `grafana-data` is a separate deliberate act.

### 2.6 Verify the whole path
```bash
aiops/scenarios/scenario.sh crashloop observe    # injects the fault into backend-sim and watches Grafana deliver the alert
aiops/scenarios/scenario.sh crashloop recover && aiops/scenarios/scenario.sh crashloop verify
```
See [aiops-demo.md](aiops-demo.md).

## 3. Controlled remediation (Stage A and B demonstrated, resting state dry run)

Stage A (approval mode with a server-side **dry run**) is the resting state on the cluster and was tested end to end (results below).
Stage B (one real, human-approved rollback of `backend-sim`) was run on 2026-10-30 and then switched back off. Each stage needs an explicit human go-ahead; autonomous mode is never configured:

```bash
# Stage A: approval flow with a SERVER-SIDE DRY RUN (adds the narrow Role: get+patch on backend-sim only)
kubectl kustomize k8s/aiops/approval-dryrun | less          # review: one extra Role+RoleBinding, two env vars
kubectl apply -k k8s/aiops/approval-dryrun
#   cause a fault with aiops/scenarios/scenario.sh <scenario> observe; the report says "Approval required"
scripts/aiops-decide.sh approve <incident-id> --approver <you> --confirm <incident-id>     # nothing changes (dry run)
# Stage B: real rollback of backend-sim after approval
kubectl apply -k k8s/aiops/approval-live
#   repeat; the Deployment is rolled back by the agent and verified; the audit log shows approver, revisions, verification
# Back to read-only at any time:
kubectl apply -k aiops/                                      # restores the default; then delete the remediator Role/RoleBinding
kubectl -n skillpulse-stg delete role,rolebinding aiops-remediator-backend-sim
```
Checks to run after each stage: `kubectl auth can-i patch deployments.apps/backend --as=system:serviceaccount:aiops:aiops-agent -n skillpulse-stg`
must be `no`; `patch deployments.apps/backend-sim` is `yes` only after stage A.

**Stage A results (measured).** The `kubectl diff` showed only the `AIOPS_MODE` change plus one Role and RoleBinding.
The authoritative permission list for the agent in `skillpulse-stg` gained exactly one write rule:
`deployments.apps [backend-sim] [get patch]`. Real server-side dry-run patches *as the agent identity* were accepted for
`backend-sim` and answered `Forbidden` for `backend`, `frontend`, the MySQL StatefulSet, production, `delete` and `scale`.
`aiops/scenarios/tools/approval_check.py` then ran against live pending incidents:

| Check | Result |
|---|---|
| Report | "Approval required" section with the exact command; states the action is a server-side dry run |
| Before approval | Incident awaiting approval, nothing executed; rollback target proven healthy (`was_ready: True`) |
| Invalid approvals | No token / garbage token / the webhook token: 401. Missing or blank approver, invalid decision, injected command in the decision field: 400. Malformed id: 400. Unknown id: 409. Incident stayed pending and the Deployment was untouched |
| Approve (valid token plus tamper fields `action=delete_pvc`, `namespace=skillpulse-prd`, `workload=mysql`) | HTTP 200; extra fields ignored; executed `rollout_undo` as a **dry run** on `skillpulse-stg/backend-sim` revision 21 to 20; verification skipped; outcome "dry-run only: no change made" |
| Dry run changed nothing | Same spec hash, generation, replicas, revision and ReplicaSet scaling before and after |
| Reject (through `scripts/aiops-decide.sh`) | HTTP 200; `approval_rejected` by the named approver; no action, no verification; Deployment unchanged; a second decision is refused (409) |
| Restart | A pending approval survived `kubectl rollout restart` of the agent: the new pod's checkpoint was still paused at `await_approval` |
| Audit | 140 events, chain verified; one executed action in the whole log: the dry run; no real execution ever |

**Stage B results (measured, 2026-10-30).** `kubectl diff -k k8s/aiops/approval-live` showed exactly one change: `AIOPS_DRY_RUN: "true" -> "false"`
(`auto_remediation_enabled` stayed `false`; the write allowlist stayed `backend-sim` only). A real crash loop was injected into
`backend-sim`: Grafana's alert reached the agent in **119.3 s**; the report diagnosed `crash_loop` / `invalid_configuration`
(`PORT: 8080 -> notaport`), proved the rollback target (revision 24) had been Ready, and the policy decision was `requires_approval`.
Before approving, the target, rollback revision, agent RBAC (only `patch backend-sim` allowed) and the unchanged generations of
every real stg/prd workload were re-checked. After the human approval, the agent executed **one** `rollout_undo` (revision 25 to 24)
in the same second and recorded `recovered: true` 10 s later. The audit chain stayed valid. The agent was then set back to
`approval` / `dry_run=true`.

Limitation worth knowing: a `patch` grant on one named Deployment lets *any* caller holding the agent's credentials patch any field of
`backend-sim` (RBAC cannot restrict fields). The agent only uses typed calls from a closed action registry, but this is why the grant is
limited to a throw-away workload and why it must never be extended to a real application.

## 4. GitOps with Argo CD and Sealed Secrets  **(installed for the sandbox only)**

Everything below is written and validated offline (and rehearsed on a disposable cluster, section 4.1a): manifests, overlays,
tests. **Argo CD and Sealed Secrets are installed and the sandbox was synced once** (results in 4.1a and in the architecture doc, section 9); staging and production-like are not under Argo CD yet.

### 4.1 Argo CD migration review (read this before syncing stg or prd)

Argo CD "adopts" an object when it first syncs a manifest with the same name and namespace. Assessment of this
repository, from real read-only checks:

| Question | Finding |
|---|---|
| Can Argo CD render the existing overlays? | **No**: `k8s/overlays/*` read the git-ignored `db-secret.env`. The `k8s/gitops/*` overlays are the Argo CD sources (same base, no `secretGenerator`) |
| Is the GitOps render equal to what runs? | **Yes**: spec-identical to the live overlays for every object except the generated Secret (`aiops/tests/test_gitops_manifests.py` proves it when the local env files exist) |
| What would the first sync change? | `kubectl diff -k k8s/gitops/stg` and `prd` (server-side, read-only) show **one added annotation per object** (`argocd.argoproj.io/sync-options: Prune=false,Delete=false`), 7 objects each, and nothing else: no pod-template change, so **no restarts**, including MySQL |
| Risk: StatefulSet and PVCs | `volumeClaimTemplates` are identical to the live ones, so there is no immutable-field error. PVCs are not in the manifests and the project does not allow deleting anything (`Prune=false`, `Delete=false`) |
| Risk: image tag | The base uses `trainwithshubham/*:latest`. Argo CD will not notice a new image behind `latest`. Pin images in the overlays when CI is wired in |
| Risk: Secret ownership | The Sealed Secrets controller refuses to overwrite an existing Secret. Use `--adopt-existing` once, deliberately (4.3) |
| Risk: Argo CD's own power | The application controller's and the server's ClusterRoles are `* * *` (**cluster-admin equivalent**, the chart default, verified live: both can read Secrets in every namespace); Argo CD is therefore a high-value target: keep it port-forward-only and restrict who can reach it. The `AppProject` is the limiting control: one namespace (`skillpulse-gitops`; stg and prd are added only after a separate review), no cluster-scoped kinds, no plaintext `Secret` |
| Risk: tracking | Tracking is by annotation (`argocd.argoproj.io/tracking-id`), set in the install values. The agent refuses to modify any Deployment carrying that metadata |
| Applications are manual-sync | No `automated` block, no self-heal, no prune, no finalizer; tests fail if any of that appears |

Recommended order: **sandbox first** (a new namespace, adopts nothing), then stg after the review, then prd.

### 4.1a Sandbox preflight (measured on a disposable kind cluster, 2026-10-30)

Both installs were rehearsed on a throw-away `kind` cluster with its own kubeconfig; the `skillpulse` cluster was not touched.

| Item | Result |
|---|---|
| Argo CD 10.11.6 (app v3.5.6) | `helm install --wait` took about 90 s; 4 pods + 1 completed init Job, 0 restarts; 3 CRDs, 2 ClusterRoles, 2 ClusterRoleBindings, 4 NetworkPolicies. Requests 225m CPU / 576Mi (limits 1152Mi) |
| Argo CD cluster-wide power | The application-controller ClusterRole uses wildcards. The `AppProject` is the control: a test Application aimed at `kube-system` was refused (`InvalidSpecError ... do not match any of the allowed destinations`); `Secret` and cluster-scoped kinds are not whitelisted |
| Manual sync | Before the first sync the Application was `OutOfSync` and **created nothing**. One manual sync created the 7 sandbox objects (tracked by annotation, each with `Prune=false,Delete=false`). **Deleting the Application removed none of them** (no cascade) |
| Sealed Secrets, chart default | A ClusterRole with get/list/create/update/**delete** on Secrets in every namespace. **Rejected** |
| Sealed Secrets, scoped (`sealed-secrets-values.yaml`) | No ClusterRole; namespaced Roles in `sealed-secrets` and `skillpulse-gitops` only; controller Ready with 0 errors. Installing it in `kube-system` fails (it needs to list in its own namespace), hence its own namespace |
| Seal / unseal | Sealed with `kubeseal` 0.41.2 (checksum-verified): no plaintext in the file; unsealed in `skillpulse-gitops`; the same ciphertext replayed into another namespace produced **no** Secret |
| Uninstall | `helm uninstall` removes the workloads and ClusterRoles but **keeps the CRDs** (so Applications survive) and any already-unsealed Secrets and synced workloads |
| Repository access | The repo is public, so no credentials are needed. `main` has no `k8s/gitops` yet, so the sandbox Application tracks `feature/aiops-self-healing` until the merge |

Rollback: `kubectl -n argocd delete application skillpulse-sandbox` (no cascade), `helm uninstall argocd -n argocd`,
`helm uninstall sealed-secrets -n sealed-secrets`; only after that, deliberately: delete the CRDs
(`kubectl delete crd applications.argoproj.io applicationsets.argoproj.io appprojects.argoproj.io sealedsecrets.bitnami.com`),
then the namespaces `argocd`, `sealed-secrets` and `skillpulse-gitops` (the last one holds the sandbox MySQL volume).

### 4.2 Install (each step needs approval)
```bash
# 0. Required namespaces FIRST. The scoped Sealed Secrets chart creates a Role inside `skillpulse-gitops`, so that namespace
#    must exist before the helm install (otherwise: "namespaces skillpulse-gitops not found" and a crash-looping controller).
#    Created by hand because the AppProject allows no cluster-scoped resources.
kubectl config current-context                                    # kind-skillpulse
kubectl create namespace skillpulse-gitops
python3 gitops/validate_install_order.py                          # offline: every namespace the charts need is created first

# 1. Sealed Secrets, SANDBOX-SCOPED (the chart default is a ClusterRole with get/list/create/update/DELETE on Secrets in every
#    namespace; gitops/argocd/install/sealed-secrets-values.yaml removes it: namespaced Roles in `sealed-secrets` and
#    `skillpulse-gitops` only, own namespace, no kube-system). Validated on a disposable kind cluster.
helm install sealed-secrets oci://registry-1.docker.io/bitnamicharts/sealed-secrets --version 2.22.0 \
  -n sealed-secrets --create-namespace -f gitops/argocd/install/sealed-secrets-values.yaml
brew install kubeseal        # or download kubeseal 0.41.2 from the GitHub release and verify its checksum

# 2. Argo CD: 4 pods, 225m CPU / 576Mi requested, 3 CRDs, 2 ClusterRoles (the install takes several minutes on a busy node)
helm install argocd argo/argo-cd --version 10.11.6 -n argocd --create-namespace -f gitops/argocd/install/argocd-values.yaml
kubectl -n argocd rollout status deployment/argocd-server
kubectl -n argocd port-forward svc/argocd-server 8081:80 &      # initial admin password: kubectl -n argocd get secret argocd-initial-admin-secret

# 3. AppProject and the (manual-sync) sandbox Application
kubectl apply -f gitops/argocd/project.yaml
kubectl apply -f gitops/argocd/apps/skillpulse-sandbox.yaml
```
The repository must contain `k8s/gitops/**` on the branch the Applications track (`main`): merge before syncing.

### 4.3 Secrets
```bash
scripts/seal-db-secret.sh sandbox                  # new namespace: nothing to adopt
scripts/seal-db-secret.sh stg --adopt-existing     # takes over the existing skillpulse-db in skillpulse-stg
```
Commit the generated `k8s/gitops/secrets/<env>/` (encrypted), then sync it with its own Application. Tests refuse any
plaintext Secret in the tree, and the AppProject cannot sync one.

### 4.4 Sync, approval and rollback (always manual)
```bash
argocd app diff skillpulse-sandbox          # review first
argocd app sync skillpulse-sandbox
argocd app history skillpulse-stg           # revisions
argocd app rollback skillpulse-stg <id>     # or: git revert + merge + sync (preferred: Git stays the source of truth)
```
Configuration changes proposed by the agent (`propose_config_pr`) arrive as pull requests; merge, then sync by hand.

## 5. Troubleshooting quick reference

| Symptom | Likely cause and fix |
|---|---|
| Agent returns 401 to Grafana | Token mismatch: compare hashes (`kubectl -n X get secret aiops-webhook -o jsonpath='{.data.token}' \| base64 -d \| shasum -a 256`) in `monitoring` and `aiops`; recreate with `printf %s` (no newline) |
| Grafana pod `CreateContainerConfigError` | The Secret `monitoring/aiops-webhook` is missing |
| Helm upgrade: `rollingUpdate: Forbidden` | Strategy change vs defaulted live field (see monitoring README); test with the direct server-side dry run |
| No alert although the pod crashes | Faults shorter than about 2 minutes can slip through; check rule state in Grafana → Alerting; check `max_over_time` expression in Explore |
| Incident says "workload not found" | Agent RBAC covers only stg and prd; the alert's namespace or pod label is wrong |
| Report shows "Collection problems" | Prometheus or Loki unreachable; the agent fell back to Kubernetes logs |
| `kind load` image not used | The Deployment must reference the exact tag and `imagePullPolicy: IfNotPresent` |
| Port 3000 / 18085 already in use | A leftover `kubectl port-forward`; list with `ps aux \| grep port-forward` |
