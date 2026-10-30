#!/usr/bin/env bash
# Back up the Sealed Secrets controller's private sealing key(s) to an ENCRYPTED file OUTSIDE the repository.
#
#   scripts/sealed-secrets-key-backup.sh backup   [DEST_DIR]   default: ~/secure-backups/sealed-secrets
#   scripts/sealed-secrets-key-backup.sh verify   FILE.gpg     decrypts to a pipe and only counts keys (nothing shown or written)
#   scripts/sealed-secrets-key-backup.sh restore-help          prints the (manual) restore procedure
#
# NOT RUN YET. Without this key every SealedSecret in Git becomes undecryptable if the controller's namespace is lost.
# The key never touches the terminal or the repository: it flows kubectl -> gpg (AES-256, passphrase typed into gpg's own
# prompt, never on a command line) -> a mode-600 file. Store the passphrase in a password manager, and copy the .gpg file
# to a second location (e.g. an encrypted external disk). Whoever holds the key AND the repo can decrypt every SealedSecret.
set -euo pipefail
umask 077

CTX=kind-skillpulse; NS=sealed-secrets; LABEL=sealedsecrets.bitnami.com/sealed-secrets-key
[ "$(kubectl config current-context)" = "$CTX" ] || { echo "context must be $CTX" >&2; exit 1; }
command -v gpg >/dev/null || { echo "gpg not found" >&2; exit 1; }

case "${1:?usage: $0 backup [DEST]|verify FILE|restore-help}" in
  backup)
    DEST="${2:-$HOME/secure-backups/sealed-secrets}"
    case "$(cd "$(dirname "$DEST")" 2>/dev/null && pwd)/$(basename "$DEST")" in "$(git rev-parse --show-toplevel 2>/dev/null || echo /nonexistent)"*) echo "DEST must be outside the repository" >&2; exit 1 ;; esac
    mkdir -p "$DEST"; chmod 700 "$DEST"
    OUT="$DEST/sealed-secrets-key-$(date +%Y%m%d-%H%M%S).yaml.gpg"
    N="$(kubectl -n "$NS" get secret -l "$LABEL" -o name | wc -l | tr -d ' ')"; [ "$N" -ge 1 ] || { echo "no sealing key found" >&2; exit 1; }
    kubectl -n "$NS" get secret -l "$LABEL" -o yaml | gpg --symmetric --cipher-algo AES256 --output "$OUT"
    chmod 600 "$OUT"; shasum -a 256 "$OUT" > "$OUT.sha256"
    echo "wrote $OUT ($N key secret(s), $(wc -c < "$OUT") bytes) and $OUT.sha256; content not shown"
    echo "next: '$0 verify $OUT', then copy the file to a second location" ;;
  verify)
    F="${2:?file}"; [ -f "$F" ] || { echo "no such file" >&2; exit 1; }
    gpg --decrypt "$F" 2>/dev/null | grep -c '^kind: Secret' | xargs -I{} echo "decrypts OK: {} Secret object(s) inside (content not displayed)"
    live="$(kubectl -n "$NS" get secret -l "$LABEL" -o name | wc -l | tr -d ' ')"; echo "live key secrets in cluster: $live" ;;
  restore-help)
    cat <<'EOF'
Restore (only after the controller's namespace/keys were lost; do this BEFORE (re)starting the controller if possible):
  1. kubectl create namespace sealed-secrets           # if missing
  2. gpg --decrypt FILE.gpg | kubectl apply -f -        # key Secret(s) are re-created; passphrase typed into gpg's prompt
  3. helm upgrade --install sealed-secrets ... -f gitops/argocd/install/sealed-secrets-values.yaml   # (same values as install)
  4. kubectl -n sealed-secrets rollout restart deployment/sealed-secrets-controller   # it loads all key Secrets on start
  5. kubeseal --controller-name sealed-secrets-controller --controller-namespace sealed-secrets --validate < k8s/gitops/secrets/sandbox/sealed-secret.yaml
Rotate the key itself (a new key is generated every 30 days by default; old keys are kept so old SealedSecrets still decrypt):
  back up AGAIN after each new key appears, e.g. monthly.
EOF
    ;;
esac
