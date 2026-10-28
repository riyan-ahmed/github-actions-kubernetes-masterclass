# Argo CD for SkillPulse (written and validated; **not installed**)

| File | Purpose |
|---|---|
| `install/argocd-values.yaml` | Minimal Argo CD (chart `argo/argo-cd` 10.11.6): no Dex, no notifications, no ApplicationSet pods, no web terminal, annotation tracking |
| `project.yaml` | `AppProject skillpulse`: one repo, three namespaces, **no cluster-scoped resources**, no plaintext `Secret` |
| `apps/skillpulse-sandbox.yaml` | Non-adopting: deploys into a new namespace `skillpulse-gitops`. Sync this first |
| `apps/skillpulse-stg.yaml`, `apps/skillpulse-prd.yaml` | Adopt the existing workloads on first sync: read the migration review first |

All Applications are **manual sync**: no `automated`, no self-heal, no prune, no finalizer. Validate offline with
`make aiops-validate-gitops` (renders the overlays and checks the manifests against the chart's CRD schemas). The
migration review, install steps, secrets approach and rollback are in
[`docs/aiops-setup.md`](../../docs/aiops-setup.md#4-gitops-with-argo-cd-and-sealed-secrets--not-yet-applied).
