#!/usr/bin/env bash
# GitOps self-healing demo for the isolated oom-demo workload (namespace skillpulse-gitops, Argo CD app oom-demo).
#
#   scripts/gitops-oom-demo.sh preflight          read-only checks: cluster, Argo CD app, agent config, agent RBAC
#   scripts/gitops-oom-demo.sh status             oom-demo pods/Deployment, Argo CD app, open agent PRs (read-only)
#   scripts/gitops-oom-demo.sh inject             Git commit: ALLOC_MB 12 -> 40 on the gitops-demo branch (a memory-demand regression)
#   scripts/gitops-oom-demo.sh reset              Git commit: back to ALLOC_MB 12 and limits.memory 32Mi
#   scripts/gitops-oom-demo.sh measure <inc-id>   stage durations of one finished run (read-only)
#
# Everything that changes the workload goes through Git + Argo CD. This script contains no kubectl write command:
# no apply, patch, edit, scale, delete, rollout undo or argocd sync (aiops/tests/test_gitops_pr_recovery.py enforces it).
# The agent's recovery PR is merged by a human in GitHub: the script never merges.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; ROOT="$(dirname "$HERE")"
CTX="${DEMO_CONTEXT:-kind-skillpulse}"; NS=skillpulse-gitops; APP=oom-demo
REPO_SLUG="${DEMO_REPO:-riyan-ahmed/github-actions-kubernetes-masterclass}"; BRANCH="${DEMO_BRANCH:-gitops-demo}"
RUNS="$ROOT/aiops/audit/gitops-runs"
K() { kubectl --context "$CTX" "$@"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1" >&2; exit 1; }; }
need kubectl; need git; need python3

git_edit() {   # git_edit <commit message> <python edit script reading $1 = checkout dir>
  local msg="$1" edit="$2" tmp; tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN
  git clone -q --branch "$BRANCH" --single-branch "https://github.com/${REPO_SLUG}.git" "$tmp/r"
  python3 -c "$edit" "$tmp/r"
  git -C "$tmp/r" diff --quiet && { echo "no change needed (already in that state)"; return 1; }
  git -C "$tmp/r" -c user.name="gitops-oom-demo" -c user.email="noreply@example.invalid" commit -q -am "$msg"
  git -C "$tmp/r" push -q origin "$BRANCH"
  git -C "$tmp/r" rev-parse HEAD
}

EDIT_ALLOC='
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]) / "k8s/gitops/oom-demo/deployment.yaml"
t = p.read_text()
t = re.sub(r"(name: ALLOC_MB, value: \")\d+(\")", r"\g<1>%s\g<2>" % ALLOC, t)
p.write_text(t)'

case "${1:-}" in
  preflight)
    [ "$(kubectl config current-context)" = "$CTX" ] || echo "note: current kubectl context is $(kubectl config current-context); using --context $CTX"
    echo "== Argo CD application"
    K -n argocd get application $APP -o jsonpath='{.spec.syncPolicy.automated}{"  sync="}{.status.sync.status}{" health="}{.status.health.status}{" rev="}{.spec.source.targetRevision}{"\n"}'
    echo "== agent configuration (mode/dry-run/github; token value is never printed)"
    K -n aiops get deploy aiops-agent -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep -E 'AIOPS_(MODE|DRY_RUN|GITHUB_ENABLED|GITHUB_REPO|GITHUB_BASE_BRANCH|LLM_PROVIDER)='
    K -n aiops get deploy aiops-agent -o jsonpath='image={.spec.template.spec.containers[0].image}{"\n"}'
    echo "== agent permissions on the GitOps workload (every write must be 'no')"
    SA="system:serviceaccount:aiops:aiops-agent"; bad=0
    for v in create update patch delete; do for r in deployments.apps pods replicasets.apps; do
      a="$(K auth can-i "$v" "$r" -n $NS --as "$SA" 2>/dev/null || true)"; printf '  %-7s %-16s %s\n' "$v" "$r" "$a"; [ "$a" = "no" ] || bad=1
    done; done
    for v in patch update delete create; do
      a="$(K auth can-i "$v" applications.argoproj.io/$APP -n argocd --as "$SA" 2>/dev/null || true)"; printf '  %-7s %-16s %s\n' "$v" "argo application" "$a"; [ "$a" = "no" ] || bad=1
    done
    a="$(K auth can-i get applications.argoproj.io/$APP -n argocd --as "$SA" 2>/dev/null || true)"; echo "  get     argo application $a  (read-only, expected yes)"
    [ "$bad" = 0 ] && echo "RESULT: the agent has no write permission on the GitOps-managed workload or its Argo CD application" \
      || { echo "RESULT: UNEXPECTED WRITE PERMISSION - stop" >&2; exit 1; } ;;
  status)
    K -n $NS get deploy,pods -l app=$APP -o wide
    K -n argocd get application $APP
    command -v gh >/dev/null && gh pr list --repo "$REPO_SLUG" --base "$BRANCH" --state all --limit 5 || true ;;
  inject)
    mkdir -p "$RUNS"; chmod 700 "$RUNS"
    sha="$(ALLOC=40 git_edit "demo: oom-demo memory demand 12 -> 40 MiB (failure injection)" "ALLOC=40
$EDIT_ALLOC")" || exit 1
    now="$(python3 -c 'import time,datetime;t=time.time();print(datetime.datetime.fromtimestamp(t,datetime.timezone.utc).isoformat())')"
    echo "{\"injected_at\": \"$now\", \"commit\": \"$sha\"}" | tee "$RUNS/last-inject.json"
    echo "Argo CD will sync this commit; watch: $0 status" ;;
  reset)
    # restore the healthy baseline: ALLOC_MB=12 and limits.memory=32Mi (the agent's PR may have raised it)
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    git clone -q --branch "$BRANCH" --single-branch "https://github.com/${REPO_SLUG}.git" "$tmp/r"
    python3 - "$tmp/r" <<'PY'
import re, sys, pathlib
root = pathlib.Path(sys.argv[1]) / "k8s/gitops/oom-demo"
d = root / "deployment.yaml"; d.write_text(re.sub(r'(name: ALLOC_MB, value: ")\d+(")', r'\g<1>12\g<2>', d.read_text()))
p = root / "patches/resources.yaml"; p.write_text(re.sub(r"(memory: )\d+Mi", r"\g<1>32Mi", p.read_text()))
PY
    git -C "$tmp/r" diff --quiet && { echo "already at baseline"; exit 0; }
    git -C "$tmp/r" -c user.name="gitops-oom-demo" -c user.email="noreply@example.invalid" commit -q -am "demo: reset oom-demo to healthy baseline (ALLOC_MB=12, limit 32Mi)"
    git -C "$tmp/r" push -q origin "$BRANCH"; echo "baseline committed: $(git -C "$tmp/r" rev-parse --short HEAD)" ;;
  measure)
    INC="${2:?usage: $0 measure <incident-id>}"
    exec python3 "$ROOT/aiops/scenarios/tools/gitops_measure.py" "$INC" --context "$CTX" --inject-file "$RUNS/last-inject.json" ;;
  *) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
