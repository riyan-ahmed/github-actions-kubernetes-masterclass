#!/usr/bin/env bash
# Turn the git-ignored k8s/overlays/<env>/db-secret.env into an ENCRYPTED SealedSecret that is safe to commit.
#
#   scripts/seal-db-secret.sh <stg|prd|sandbox> [--adopt-existing] [--env-file PATH]
#
# Requires: kubeseal and kubectl, and the Sealed Secrets controller running in the target cluster.
# The plaintext only ever travels through a pipe (kubectl create --dry-run | kubeseal); it is never written
# to disk, never printed and never placed in a command-line argument. The result is verified to contain
# encryptedData and no plaintext before the script reports success.
set -euo pipefail
umask 077

ENV_NAME="${1:?usage: $0 <stg|prd|sandbox> [--adopt-existing] [--env-file PATH]}"; shift || true
ADOPT=0; ENV_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --adopt-existing) ADOPT=1 ;;
    --env-file) ENV_FILE="${2:?--env-file needs a path}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac; shift
done
case "$ENV_NAME" in
  stg) NS=skillpulse-stg; DEFAULT_ENV=k8s/overlays/stg/db-secret.env ;;
  prd) NS=skillpulse-prd; DEFAULT_ENV=k8s/overlays/prd/db-secret.env ;;
  sandbox) NS=skillpulse-gitops; DEFAULT_ENV=k8s/overlays/stg/db-secret.env ;;   # sandbox reuses the stg values unless --env-file is given
  *) echo "environment must be stg, prd or sandbox" >&2; exit 2 ;;
esac
ENV_FILE="${ENV_FILE:-$DEFAULT_ENV}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (git-ignored credentials file)" >&2; exit 1; }
command -v kubeseal >/dev/null || { echo "kubeseal not found (brew install kubeseal)" >&2; exit 1; }

OUT_DIR="k8s/gitops/secrets/$ENV_NAME"; OUT="$OUT_DIR/sealed-secret.yaml"
mkdir -p "$OUT_DIR"
TMP="$(mktemp "$OUT_DIR/.sealed.XXXXXX")"; trap 'rm -f "$TMP"' EXIT

kubectl create secret generic skillpulse-db --namespace "$NS" --from-env-file="$ENV_FILE" --dry-run=client -o yaml \
  | kubeseal --controller-name "${SEALED_SECRETS_CONTROLLER:-sealed-secrets-controller}" \
             --controller-namespace "${SEALED_SECRETS_NAMESPACE:-kube-system}" --format yaml > "$TMP"

# refuse anything that is not an encrypted SealedSecret
grep -q '^kind: SealedSecret' "$TMP" || { echo "output is not a SealedSecret: refusing to write it" >&2; exit 1; }
grep -q 'encryptedData:' "$TMP" || { echo "no encryptedData in output: refusing to write it" >&2; exit 1; }
if grep -qE '^\s*(data|stringData):' "$TMP"; then echo "plaintext data/stringData found: refusing to write it" >&2; exit 1; fi

if [ "$ADOPT" = 1 ]; then
  # let the controller take over an EXISTING Secret named skillpulse-db (it refuses otherwise)
  python3 - "$TMP" <<'PY'
import sys, re
p = sys.argv[1]; t = open(p).read()
t = re.sub(r'(?m)^(metadata:\n)', r'\1  annotations:\n    sealedsecrets.bitnami.com/managed: "true"\n', t, count=1) if 'annotations:' not in t.split('spec:')[0] else t
open(p, 'w').write(t)
PY
fi
mv "$TMP" "$OUT"; trap - EXIT
cat > "$OUT_DIR/kustomization.yaml" <<KUS
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NS
resources:
  - sealed-secret.yaml
KUS
echo "wrote $OUT (SealedSecret, $(grep -c '^    [A-Z_]*:' "$OUT") encrypted keys) and $OUT_DIR/kustomization.yaml"
echo "next: review, commit, then create the matching Argo CD Application for k8s/gitops/secrets/$ENV_NAME (docs/aiops-setup.md)"
