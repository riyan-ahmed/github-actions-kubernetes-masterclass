#!/usr/bin/env bash
# Rotate the legacy (namespace `skillpulse`) MySQL credentials that were published in Git, in a way that is reversible
# until the final step. NOT RUN YET: every subcommand that changes the cluster needs an explicit human go-ahead.
#
#   scripts/rotate-legacy-db-credentials.sh preflight   read-only checks
#   scripts/rotate-legacy-db-credentials.sh backup      logical dump of the legacy database (read-only on the cluster)
#   scripts/rotate-legacy-db-credentials.sh gen         create NEW random credentials in a git-ignored file
#   scripts/rotate-legacy-db-credentials.sh rotate      MySQL: ALTER USER ... RETAIN CURRENT PASSWORD (old AND new work),
#                                                       then update the Secret, then restart the backend, then verify
#   scripts/rotate-legacy-db-credentials.sh finalize    MySQL: DISCARD OLD PASSWORD (point of no easy return), verify old is dead
#   scripts/rotate-legacy-db-credentials.sh rollback    (only before finalize) put the old Secret back and restart the backend
#
# Why both the database AND the Secret: MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD from the Secret are only used by the MySQL image
# on the FIRST start of an empty data directory. Changing the Secret alone changes nothing in the database.
# Passwords never appear in arguments or output: they travel through stdin and files with mode 600 under .secrets/.
set -euo pipefail
umask 077

NS=skillpulse                                   # fixed on purpose: this script must never touch another environment
CTX="${ROTATE_CONTEXT:-kind-skillpulse}"   # override only for rehearsals on a throw-away cluster
DIR=.secrets/legacy-rotation                    # git-ignored
NEW="$DIR/new.env"; OLD="$DIR/old.env"
ACTION="${1:?usage: $0 preflight|backup|gen|rotate|finalize|rollback}"

[ "$(kubectl config current-context)" = "$CTX" ] || { echo "context must be $CTX" >&2; exit 1; }
git check-ignore -q "$NEW" || { echo "$DIR is not git-ignored: refusing to continue" >&2; exit 1; }
mkdir -p "$DIR"

sql() { # sql <user> <password-file-var-name>  : SQL on stdin, password from the named env file key, passed via stdin to the pod
  local user="$1" pass="$2" stmts; stmts="$(cat)"
  { printf '%s\n' "$pass"; printf '%s\n' "$stmts"; } | kubectl -n "$NS" exec -i mysql-0 -- sh -c \
    'IFS= read -r p; MYSQL_PWD="$p" mysql -h127.0.0.1 -u'"$user"' -N'
}
val() { grep "^$2=" "$1" | cut -d= -f2- | tr -d '\n'; }
accepts() { # accepts <user> <password>
  printf '%s\n' "$2" | kubectl -n "$NS" exec -i mysql-0 -- sh -c \
    'IFS= read -r p; MYSQL_PWD="$p" mysql -h127.0.0.1 -u'"$1"' -N -e "SELECT 1" >/dev/null 2>&1' 2>/dev/null && echo ACCEPTED || echo rejected
}
backend_ok() { kubectl get --raw "/api/v1/namespaces/$NS/services/backend:8080/proxy/api/dashboard" >/dev/null && echo OK || echo FAIL; }
restart_backend() { kubectl -n "$NS" rollout restart deployment/backend && kubectl -n "$NS" rollout status deployment/backend --timeout=120s; }
apply_secret() { # apply_secret <env-file>
  kubectl -n "$NS" create secret generic skillpulse-db --from-env-file="$1" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

case "$ACTION" in
  preflight)
    kubectl -n "$NS" get pod mysql-0 -o jsonpath='{.status.containerStatuses[0].ready}{"\n"}' | grep -q true
    kubectl -n "$NS" rollout status deployment/backend --timeout=30s
    echo "backend API: $(backend_ok)"
    ls -t backups/legacy-skillpulse-*.sql >/dev/null 2>&1 && echo "latest legacy backup: $(ls -t backups/legacy-skillpulse-*.sql | head -1)" || echo "NO legacy backup yet: run 'backup'"
    [ -f "$NEW" ] && echo "new credentials file present" || echo "new credentials not generated yet: run 'gen'" ;;
  backup)
    OUT="backups/legacy-skillpulse-$(date +%Y%m%d-%H%M%S).sql"; mkdir -p backups
    kubectl -n "$NS" exec mysql-0 -- sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -h127.0.0.1 -uroot --single-transaction --no-tablespaces --routines --triggers skillpulse' > "$OUT"
    chmod 600 "$OUT"
    grep -q 'Dump completed' "$OUT" || { echo "dump incomplete: $OUT" >&2; exit 1; }
    echo "backup OK: $OUT ($(wc -c < "$OUT") bytes, $(grep -c '^CREATE TABLE' "$OUT") tables, $(grep -c '^INSERT INTO' "$OUT") insert statements)" ;;
  gen)
    [ ! -e "$NEW" ] || { echo "$NEW exists: refusing to overwrite" >&2; exit 1; }
    { echo "MYSQL_ROOT_PASSWORD=$(openssl rand -hex 24)"
      echo "MYSQL_DATABASE=$(kubectl -n "$NS" get secret skillpulse-db -o jsonpath='{.data.MYSQL_DATABASE}' | base64 -d)"
      echo "MYSQL_USER=$(kubectl -n "$NS" get secret skillpulse-db -o jsonpath='{.data.MYSQL_USER}' | base64 -d)"
      echo "MYSQL_PASSWORD=$(openssl rand -hex 24)"; } > "$NEW"
    echo "generated $NEW (mode $(stat -f %Lp "$NEW" 2>/dev/null || stat -c %a "$NEW")); values not shown" ;;
  rotate)
    [ -f "$NEW" ] || { echo "run 'gen' first" >&2; exit 1; }
    ls backups/legacy-skillpulse-*.sql >/dev/null 2>&1 || { echo "no legacy backup: run 'backup' first" >&2; exit 1; }
    [ ! -e "$OLD" ] || { echo "$OLD exists: a rotation is already in progress" >&2; exit 1; }
    # keep the CURRENT credentials (read from the live Secret) so that rollback is possible
    for k in MYSQL_ROOT_PASSWORD MYSQL_DATABASE MYSQL_USER MYSQL_PASSWORD; do
      echo "$k=$(kubectl -n "$NS" get secret skillpulse-db -o jsonpath="{.data.$k}" | base64 -d)"; done > "$OLD"
    OLD_ROOT="$(val "$OLD" MYSQL_ROOT_PASSWORD)"; NEW_ROOT="$(val "$NEW" MYSQL_ROOT_PASSWORD)"; NEW_APP="$(val "$NEW" MYSQL_PASSWORD)"; APPU="$(val "$NEW" MYSQL_USER)"
    echo "[1/4] MySQL: new passwords added, old ones RETAINED (both work)"
    printf "ALTER USER '%s'@'%%' IDENTIFIED BY '%s' RETAIN CURRENT PASSWORD;\nALTER USER 'root'@'%%' IDENTIFIED BY '%s' RETAIN CURRENT PASSWORD;\nALTER USER 'root'@'localhost' IDENTIFIED BY '%s' RETAIN CURRENT PASSWORD;\n" \
      "$APPU" "$NEW_APP" "$NEW_ROOT" "$NEW_ROOT" | sql root "$OLD_ROOT"
    echo "      app new: $(accepts "$APPU" "$NEW_APP")  app old: $(accepts "$APPU" "$(val "$OLD" MYSQL_PASSWORD)")  root new: $(accepts root "$NEW_ROOT")"
    echo "[2/4] Secret skillpulse-db updated"; apply_secret "$NEW"
    # `make secret` rebuilds the Secret from k8s/db-secret.env: it must carry the NEW values, or a later `make apply` would revert them
    LOCAL=k8s/db-secret.env; [ ! -f "$LOCAL" ] || cp -p "$LOCAL" "$DIR/db-secret.env.before-rotation"
    install -m 600 "$NEW" "$LOCAL"; git check-ignore -q "$LOCAL" || { echo "$LOCAL is not git-ignored" >&2; exit 1; }
    echo "      local credential source $LOCAL now holds the new values (git-ignored, mode 600)"
    echo "[3/4] backend restarted so it reads the new password"; restart_backend
    echo "[4/4] verify: backend API through the API-server proxy: $(backend_ok)"
    echo "Old credentials still work (RETAIN). Check the app, then run 'finalize'. To undo: 'rollback'." ;;
  finalize)
    [ -f "$NEW" ] && [ -f "$OLD" ] || { echo "nothing to finalize" >&2; exit 1; }
    [ "$(backend_ok)" = OK ] || { echo "backend API not healthy: not finalizing" >&2; exit 1; }
    APPU="$(val "$NEW" MYSQL_USER)"; NEW_ROOT="$(val "$NEW" MYSQL_ROOT_PASSWORD)"
    printf "ALTER USER '%s'@'%%' DISCARD OLD PASSWORD;\nALTER USER 'root'@'%%' DISCARD OLD PASSWORD;\nALTER USER 'root'@'localhost' DISCARD OLD PASSWORD;\n" "$APPU" | sql root "$NEW_ROOT"
    echo "old app pw: $(accepts "$APPU" "$(val "$OLD" MYSQL_PASSWORD)")  old root pw: $(accepts root "$(val "$OLD" MYSQL_ROOT_PASSWORD)")  new app pw: $(accepts "$APPU" "$(val "$NEW" MYSQL_PASSWORD)")  backend: $(backend_ok)"
    echo "Done. Keep $OLD only until you are sure; then securely delete it (it is the exposed credential)." ;;
  rollback)
    [ -f "$OLD" ] || { echo "no $OLD: nothing to roll back" >&2; exit 1; }
    echo "Secret restored to the previous values; the database still accepts them only if 'finalize' has NOT run."
    apply_secret "$OLD"; restart_backend; echo "backend API: $(backend_ok)"
    # the database now retains the FIRST new password as its secondary one, so the saved old/new files are stale: archive them,
    # and run 'gen' again before the next 'rotate'
    stamp="$(date +%Y%m%d-%H%M%S)"; mv "$OLD" "$DIR/old.env.rolled-back-$stamp"; mv "$NEW" "$DIR/new.env.rolled-back-$stamp"
    echo "state files archived in $DIR; run 'gen' then 'rotate' to try again" ;;
  *) echo "unknown action" >&2; exit 2 ;;
esac
