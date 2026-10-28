#!/usr/bin/env bash
# Reproducible failure scenarios, isolated to Deployment backend-sim in namespace skillpulse-stg.
#
#   aiops/scenarios/scenario.sh <crashloop|oom|readiness> <status|inject|observe|investigate|recover|verify>
#
#   status       show backend-sim and its revisions (read-only)
#   inject       apply the fault manifest (refuses unless backend-sim is a healthy baseline)
#   observe      inject, then watch Kubernetes/Prometheus/Grafana/agent until Grafana's alert reaches the agent
#                (no manual alert; see tools/observe.py)
#   investigate  the manual investigation commands for this scenario (read-only)
#   recover      roll back to the last healthy baseline revision (kubectl rollout undo) and wait
#   verify       check recovery: rollout, pods, baseline spec, the agent's own recovery check (read-only)
#
# Hard scope: the script can only touch skillpulse-stg/backend-sim and only with manifests from this directory.
set -euo pipefail

SCEN="${1:?usage: $0 <crashloop|oom|readiness> <status|inject|observe|investigate|recover|verify>}"
ACTION="${2:?action required}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS=skillpulse-stg; DEP=backend-sim
case "$SCEN" in
  crashloop) FAULT="$HERE/crashloop/backend-sim-v2-invalid-port.yaml"; CAUSE="v2 invalid PORT" ;;
  oom)       FAULT="$HERE/oom/backend-sim-v2-oom.yaml";                  CAUSE="v2 memory limit 6Mi" ;;
  readiness) FAULT="$HERE/readiness/backend-sim-v2-bad-probe.yaml";      CAUSE="v2 readiness probe path" ;;
  *) echo "unknown scenario $SCEN" >&2; exit 2 ;;
esac
BASELINE="$HERE/common/backend-sim-v1.yaml"
[ "$(kubectl config current-context)" = "${SCENARIO_CONTEXT:-kind-skillpulse}" ] || { echo "refusing: kubectl context is not ${SCENARIO_CONTEXT:-kind-skillpulse}" >&2; exit 1; }
K() { kubectl -n "$NS" "$@"; }

healthy_baseline() {
  [ "$(K get deploy $DEP -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null)" = "1/1" ] || return 1
  [ "$(K get deploy $DEP -o jsonpath='{.spec.template.spec.containers[0].env[2].value}')" = "8080" ] || return 1
  [ "$(K get deploy $DEP -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')" = "128Mi" ] || return 1
  [ "$(K get deploy $DEP -o jsonpath='{.spec.template.spec.containers[0].readinessProbe.httpGet.path}')" = "/health" ]
}
baseline_revision() {   # newest revision whose change-cause is the healthy baseline
  K rollout history deployment/$DEP | awk '$2=="v1" && $3=="healthy" {print $1}' | sort -n | tail -1
}
current_revision() { K get deploy $DEP -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}'; }

case "$ACTION" in
  status)
    K get deploy,pods -l app=$DEP -o wide; K rollout history deployment/$DEP; echo "baseline revision: $(baseline_revision)  current: $(current_revision)" ;;
  inject)
    healthy_baseline || { echo "refusing to inject: backend-sim is not a healthy baseline (run: $0 $SCEN recover)" >&2; exit 1; }
    K apply -f "$FAULT"; echo "injected '$CAUSE' into $NS/$DEP. Observe: $0 $SCEN investigate | recover with: $0 $SCEN recover" ;;
  observe)
    exec python3 "$HERE/tools/observe.py" "$SCEN" ;;
  investigate)
    echo "== pods"; K get pods -l app=$DEP -o wide
    echo "== rollout history and what changed"; K rollout history deployment/$DEP
    CUR="$(current_revision)"; PREV="$(K rollout history deployment/$DEP | awk 'NR>2 && NF>0 && $1!="'"$CUR"'" {print $1}' | sort -n | tail -1)"
    diff <(K rollout history deployment/$DEP --revision="$PREV") <(K rollout history deployment/$DEP --revision="$CUR") | grep -E '^[<>].*(PORT|Limits|memory|Readiness|http-get)' || true
    echo "== events (newest last)"; K get events --field-selector involvedObject.kind=Pod --sort-by=.lastTimestamp | grep $DEP | tail -8 || true
    case "$SCEN" in
      crashloop) echo "== previous container logs (expect: listen tcp: lookup tcp/notaport: unknown port)"
        for p in $(K get pods -l app=$DEP -o name); do K logs "$p" --previous 2>/dev/null | tail -4 || true; done ;;
      oom) echo "== last termination (expect reason OOMKilled, exit code 137) and the memory limit"
        K get pods -l app=$DEP -o jsonpath='{range .items[*]}{.metadata.name}{"  lastState="}{.status.containerStatuses[0].lastState.terminated.reason}{" exit="}{.status.containerStatuses[0].lastState.terminated.exitCode}{"  limit="}{.spec.containers[0].resources.limits.memory}{"\n"}{end}' ;;
      readiness) echo "== probe failures (expect: Readiness probe failed ... statuscode: 404) and endpoints"
        K get events --field-selector reason=Unhealthy | grep $DEP | tail -3 || true
        K get deploy $DEP -o jsonpath='available={.status.availableReplicas} updated={.status.updatedReplicas} desired={.spec.replicas}{"\n"}' ;;
    esac ;;
  recover)
    REV="$(baseline_revision)"
    if [ -n "$REV" ] && [ "$REV" != "$(current_revision)" ]; then
      echo "rolling back to revision $REV (healthy baseline)"; K rollout undo deployment/$DEP --to-revision="$REV"
    elif [ -z "$REV" ]; then
      echo "no baseline revision in history: re-applying the baseline manifest"; K apply -f "$BASELINE"
    else echo "already at the baseline revision"; fi
    K rollout status deployment/$DEP --timeout=180s ;;
  verify)
    K rollout status deployment/$DEP --timeout=120s
    if healthy_baseline; then echo "PASS backend-sim spec equals the healthy baseline and is 1/1 ready"; else echo "FAIL not at a healthy baseline" >&2; exit 1; fi
    BAD="$(K get pods -l app=$DEP -o json | python3 -c '
import json, sys
for p in json.load(sys.stdin)["items"]:
    if p["metadata"].get("deletionTimestamp"):          # a pod that is being deleted is not a failure
        continue
    cs = (p["status"].get("containerStatuses") or [{}])[0]
    bad = ((cs.get("state") or {}).get("waiting") or {}).get("reason") or ((cs.get("lastState") or {}).get("terminated") or {}).get("reason")
    if bad in ("CrashLoopBackOff", "OOMKilled", "Error", "StartError", "CreateContainerError"):
        print(p["metadata"]["name"], bad)
')"
    if [ -z "$BAD" ]; then echo "PASS no failing container state"; else echo "FAIL $BAD" >&2; exit 1; fi
    kubectl -n aiops exec deploy/aiops-agent -- python -m aiops.agent verify --namespace $NS --workload $DEP | python3 -c 'import json,sys; d=json.load(sys.stdin); print("agent recovery check: recovered =", d["recovered"]); sys.exit(0 if d["recovered"] else 1)' ;;
  *) echo "unknown action $ACTION" >&2; exit 2 ;;
esac
