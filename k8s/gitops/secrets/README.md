# Secrets for the GitOps overlays (sealed, never plaintext)

`k8s/overlays/*/db-secret.env` is git-ignored and Argo CD cannot read it. The database credentials reach
the cluster as a **SealedSecret**: an encrypted manifest that only the Sealed Secrets controller in the
target cluster can decrypt, so it is safe to commit.

Nothing in this directory is a Secret yet. Generate the encrypted file once the controller is installed:

```bash
scripts/seal-db-secret.sh stg            # writes k8s/gitops/secrets/stg/sealed-secret.yaml (+ kustomization.yaml)
scripts/seal-db-secret.sh stg --adopt-existing   # when a Secret named skillpulse-db already exists in the namespace
```

Rules enforced by tests (`aiops/tests/test_gitops_manifests.py`): every file under `secrets/*/` is a
`SealedSecret` with `encryptedData` only; no `kind: Secret`, `data:`, or `stringData:` anywhere in `k8s/gitops`.
The Argo CD `AppProject` does not whitelist the `Secret` kind, so a plaintext Secret could not be synced even
if one were committed by mistake.

Why `--adopt-existing`: the controller refuses to overwrite a Secret it did not create. The flag adds the
annotation `sealedsecrets.bitnami.com/managed: "true"` so it takes over the existing `skillpulse-db`. Review
`docs/aiops-setup.md` (Argo CD migration review) before using it on stg or prd.
