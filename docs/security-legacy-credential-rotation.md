# Legacy database credential rotation (prepared, not executed)

Status (2026-10-30): **tracked files cleaned, legacy backup taken, rotation NOT yet executed.** Findings measured on `kind-skillpulse`.
Done in Git (uncommitted until approved): the plaintext Secret is gone from `k8s/10-mysql.yaml` (`make secret` creates it from the git-ignored
`k8s/db-secret.env`), `make mysql` reads the password from the pod, `db.go` has no default password, examples and docs use placeholders,
and a test (`test_no_known_leaked_credential_is_tracked_anywhere`, hashes only) fails if the leaked values ever return.
Remaining: finalize (section 4, step 6) and the sealing-key backup (section 7).

## 1. What is exposed

Two demo credentials are committed in the public repository (present since the first commits, in `origin/main`):

| Credential | Tracked in |
|---|---|
| App DB password (user `skillpulse`) | `.env.example`, `Makefile` (`make mysql`), `backend/database/db.go` (hard-coded **default**), `k8s/10-mysql.yaml` (`stringData`), two docs |
| MySQL root password | `.env.example`, `k8s/10-mysql.yaml` (`stringData`), two docs |

(Values are deliberately not repeated here. Find them with `git grep -n 'DB_PASSWORD\|MYSQL_ROOT_PASSWORD'`.)

## 2. Where they are live

| Environment | Uses the exposed values? |
|---|---|
| `skillpulse` (legacy demo) | **Yes, both.** The live Secret holds them and **the database accepts both** (tested over stdin; a wrong-password control was rejected). `root` is allowed from any host (`root@%`) |
| `skillpulse-dev`, `-stg`, `-prd`, `-gitops` | **No.** Different values in the Secrets and in the local env files; both exposed values were **rejected** by each database |

Dependants of the legacy credentials: `Secret skillpulse-db`, `StatefulSet mysql` (`envFrom`; used only on first start of an empty
data directory), `Deployment backend` (`DB_USER/DB_PASSWORD/DB_NAME` from the Secret, read at pod start), `make mysql`,
`make deploy` (applies `k8s/10-mysql.yaml`, **which would overwrite a rotated Secret with the exposed value**), the hard-coded
fallback in `db.go`. Not dependent: CI workflows, Argo CD, the AIOps agent, monitoring.
Data at stake: 2 tables (5 skills, 9 log rows) on a `Delete`-reclaim 1Gi PVC.

## 3. Backups: current state

* No backup of the legacy database exists. The two files in `backups/` are `skillpulse-prd` dumps (3 days old, valid: 2 tables,
  complete marker, mode 644, git-ignored).
* `scripts/rotate-legacy-db-credentials.sh backup` takes a mode-600 dump of the legacy database and refuses to continue without it.

## 4. Rotation sequence (zero downtime, reversible until step 6)

Key fact, rehearsed on MySQL 8.4: `ALTER USER ... RETAIN CURRENT PASSWORD` keeps the old password working next to the new one until
`DISCARD OLD PASSWORD`. Changing only the Kubernetes Secret changes nothing in MySQL.

1. **Fix the source of re-exposure first** (see section 6, items 1 and 2) so `make deploy` cannot re-apply the old Secret.
2. `backup` (dump + verify).  3. `gen` (new random values in `.secrets/legacy-rotation/new.env`, mode 600, git-ignored).
4. `rotate`: MySQL gets the new passwords with the old retained, the Secret is updated, the backend is restarted, the API is checked.
5. Verify (section 5). Both old and new still work, so any problem can be undone with `rollback`.
6. `finalize`: `DISCARD OLD PASSWORD`; the exposed values stop working; verify they are rejected and the app still works.
7. Delete `.secrets/legacy-rotation/old.env` (it is the exposed credential) once satisfied.

The MySQL pod itself is not restarted. Its environment still shows the old values until its next restart; that restart is safe for
data (PVC) but is a separate, optional approval.

### Rehearsal (throw-away kind cluster, the legacy manifests unchanged, 2026-10-30)

`preflight`, `backup` (valid dump), `gen`, `rotate` (new and old both accepted; Secret updated; backend restarted; API OK), `rollback`
(old Secret restored; app healthy), a second `gen` + `rotate`, then `finalize` (old app and root passwords rejected, new accepted, API OK).
Findings that changed the script: rejected passwords printed a harmless `exit code 1` from `kubectl exec` (silenced); after a rollback the
saved state files are stale, so `rollback` now archives them and a fresh `gen` is required.

## 5. Verification and recovery

* Verify: new app and root passwords accepted; old ones rejected after `finalize`; backend `/api/dashboard` through the API-server proxy;
  `kubectl -n skillpulse get pods` unchanged; staging, production-like, sandbox untouched.
* Recovery before `finalize`: `rollback` restores the old Secret and restarts the backend (the database still accepts the old password).
* Recovery after `finalize`: `ALTER USER` back using the new root password, or restore the dump (`mysql < backups/legacy-skillpulse-*.sql`).
  Worst case the data (14 rows) is re-created from `mysql/init.sql`.

## 6. Removing hard-coded credentials from tracked files (items 1-4 done, item 5 skipped on purpose: no new tooling)

1. `k8s/10-mysql.yaml`: remove the plaintext `stringData` Secret; create the Secret from a git-ignored env file (as the overlays do).
2. `Makefile` `mysql` target: read the password from the pod (`MYSQL_PWD="$$MYSQL_PASSWORD"`), not from the command line.
3. `backend/database/db.go`: remove the default password so the service fails fast when `DB_PASSWORD` is unset (needs an image rebuild).
4. `.env.example` and the docs: placeholders (`CHANGE_ME`), never working values.
5. Add a secret scanner (gitleaks) as a pre-commit hook and CI step, with the demo strings on a deny list.
6. Do **not** rewrite public Git history: after rotation the exposed values are dead, and a force-push to a public repo does not
   recall copies. Rotation is the fix; history rewriting is optional hygiene.
7. Longer term: retire the legacy `skillpulse` namespace (needs its own approval: data deletion).

## 7. Sealed Secrets key backup

`scripts/sealed-secrets-key-backup.sh backup` pipes the key Secret through `gpg` (AES-256, passphrase typed into gpg's own prompt)
into a mode-600 file **outside the repo**; `verify` decrypts to a pipe and only counts objects; `restore-help` prints the restore steps.
Back up again after each new key (default rotation: 30 days). Store the passphrase in a password manager and keep a second copy of the
file elsewhere.

## 8. Risks

* **Image availability:** `trainwithshubham/skillpulse-backend:latest` is not pullable from the registry (`NotFound`); it works on the
  main cluster only because the image was loaded locally and the pods use `IfNotPresent`. A node rebuild or image prune would leave
  every backend (including the sandbox) in `ImagePullBackOff`. Publish or pin the image before relying on it.

* Backend connections: pods holding old pooled connections keep working; new ones need the new password, hence the restart in step 4
  (about a few seconds, single replica).
* `k8s/10-mysql.yaml` re-apply would silently revert the Secret (mitigated by step 1 of the sequence).
* `kubectl apply` stores the Secret contents in the `last-applied-configuration` annotation of every environment's Secret: this is the same
  audience as `get secret`, but remember it when granting read access.
* Root is reachable from any pod (`root@%`). Restricting it to localhost is a worthwhile follow-up (separate approval).
* The sealing key is a single point of failure until backed up.
