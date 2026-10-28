#!/usr/bin/env bash
# Human side of the AIOps agent: list incidents, read a report, approve or reject a pending remediation.
#
#   scripts/aiops-decide.sh list
#   scripts/aiops-decide.sh report  <incident-id>
#   scripts/aiops-decide.sh approve <incident-id> --approver NAME --confirm <incident-id>
#   scripts/aiops-decide.sh reject  <incident-id> --approver NAME --confirm <incident-id>
#
# Talks to the agent through a short-lived `kubectl port-forward`. The tokens are read from the cluster
# Secrets into shell variables and handed to curl through a config on stdin: they are never printed,
# never put on a command line, never written to disk. --confirm must repeat the incident id (typo guard).
set -euo pipefail

CMD="${1:?usage: $0 list|report|approve|reject ...}"; shift
ID=""; APPROVER=""; CONFIRM=""
case "$CMD" in report|approve|reject) ID="${1:?incident id required}"; shift ;; esac
while [ $# -gt 0 ]; do
  case "$1" in
    --approver) APPROVER="${2:?--approver needs a name}"; shift ;;
    --confirm) CONFIRM="${2:?--confirm needs the incident id}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac; shift
done
if [ -n "$ID" ] && ! [[ "$ID" =~ ^inc-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$ ]]; then echo "malformed incident id" >&2; exit 2; fi
if [ "$CMD" = approve ] || [ "$CMD" = reject ]; then
  [ -n "$APPROVER" ] || { echo "--approver NAME is required (it is recorded in the audit log)" >&2; exit 2; }
  [ "$CONFIRM" = "$ID" ] || { echo "--confirm must repeat the incident id exactly; nothing was sent" >&2; exit 2; }
fi

PORT="${AIOPS_LOCAL_PORT:-18086}"
kubectl -n aiops port-forward svc/aiops-agent "$PORT:8085" >/dev/null 2>&1 &
PF=$!; trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 || { echo "agent not reachable through port-forward" >&2; exit 1; }

secret() { kubectl -n aiops get secret "$1" -o jsonpath='{.data.token}' | base64 -d; }
authcfg() { printf 'header = "Authorization: Bearer %s"\n' "$1"; }
BASE="http://127.0.0.1:$PORT"

case "$CMD" in
  list)   curl -fsS -m 10 -K <(authcfg "$(secret aiops-webhook)") "$BASE/incidents"; echo ;;
  report) curl -fsS -m 10 -K <(authcfg "$(secret aiops-webhook)") "$BASE/incidents/$ID/report" | python3 -c 'import json,sys; print(json.load(sys.stdin)["report"])' ;;
  approve|reject)
    DECISION="$CMD"
    BODY="$(python3 -c 'import json,sys; print(json.dumps({"decision": sys.argv[1], "approver": sys.argv[2]}))' "$DECISION" "$APPROVER")"
    curl -sS -m 180 -K <(authcfg "$(secret aiops-approval)") -X POST "$BASE/incidents/$ID/decision" \
         -H 'Content-Type: application/json' -d "$BODY" -w '\nHTTP %{http_code}\n' ;;
  *) echo "unknown command $CMD" >&2; exit 2 ;;
esac
