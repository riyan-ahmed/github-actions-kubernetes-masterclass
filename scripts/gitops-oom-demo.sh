#!/usr/bin/env bash
# GitOps self-healing demo for the isolated oom-demo workload (namespace skillpulse-gitops, Argo CD app oom-demo).
#
#   scripts/gitops-oom-demo.sh preflight          read-only checks: cluster, Argo CD app, agent config, agent RBAC
#   scripts/gitops-oom-demo.sh status             oom-demo pods/Deployment, Argo CD app, open agent PRs (read-only)
#   scripts/gitops-oom-demo.sh inject             PR + merge: ALLOC_MB 12 -> 40 on gitops-demo (a memory-demand regression)
#   scripts/gitops-oom-demo.sh reset              PR + merge: back to ALLOC_MB 12 and limits.memory 32Mi
#   scripts/gitops-oom-demo.sh measure <inc-id>   stage durations of one finished run (read-only)
#
# gitops-demo is a protected branch (pull request + required check "validate-oom-demo", enforced for administrators too), so
# inject and reset are pull requests that must pass the same CI as the agent's PRs; there is no direct push and no --admin merge.
# Everything that changes the workload goes through Git + Argo CD. This script contains no kubectl write command:
# no apply, patch, edit, scale, delete, rollout undo or argocd sync (aiops/tests/test_gitops_pr_recovery.py enforces it).
# The agent's recovery PR is merged by a human in GitHub: the script never merges.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; ROOT="$(dirname "$HERE")"
CTX="${DEMO_CONTEXT:-kind-skillpulse}"; NS=skillpulse-gitops; APP=oom-demo
REPO_SLUG="${DEMO_REPO:-riyan-ahmed/skillpulse-gitops-self-healing}"; BRANCH="${DEMO_BRANCH:-gitops-demo}"
RUNS="$ROOT/aiops/audit/gitops-runs"
K() { kubectl --context "$CTX" "$@"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1" >&2; exit 1; }; }
need kubectl; need git; need python3

git_change_via_pr() {   # git_change_via_pr <name> <commit message> <python edit script reading $1 = checkout dir>
  local name="$1" msg="$2" edit="$3" tmp br num sha merged
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN
  br="harness/${name}-$(date +%s)"
  git clone -q --branch "$BRANCH" --single-branch "https://github.com/${REPO_SLUG}.git" "$tmp/r"
  git -C "$tmp/r" checkout -q -b "$br"
  python3 -c "$edit" "$tmp/r"
  git -C "$tmp/r" diff --quiet && { echo "no change needed (already in that state)" >&2; return 3; }
  git -C "$tmp/r" -c user.name="gitops-oom-demo" -c user.email="noreply@example.invalid" commit -q -am "$msg"
  git -C "$tmp/r" push -q origin "$br"
  sha="$(git -C "$tmp/r" rev-parse HEAD)"
  num="$(gh pr create --repo "$REPO_SLUG" --base "$BRANCH" --head "$br" --title "$msg" \
        --body "Test-harness change to the isolated oom-demo workload (not an agent PR). Must pass the same required check." | sed 's#.*/##')"
  # wait for the required check to pass (never merge around it)
  local state=""
  for _ in $(seq 1 60); do
    state="$(gh api "repos/${REPO_SLUG}/commits/${sha}/check-runs" -q '[.check_runs[] | select(.name=="validate-oom-demo") | .conclusion // "pending"][0] // "absent"')"
    [ "$state" = "success" ] && break
    [ "$state" = "failure" ] && { echo "required check failed on PR #$num" >&2; return 1; }
    sleep 5
  done
  [ "$state" = "success" ] || { echo "required check did not pass in time (state=$state) on PR #$num" >&2; return 1; }
  gh pr merge "$num" --repo "$REPO_SLUG" --merge --match-head-commit "$sha" >/dev/null
  merged="$(gh api "repos/${REPO_SLUG}/pulls/${num}" -q '.merged_at + " " + .merge_commit_sha + " " + .html_url')"
  echo "$merged"
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
    echo "== branch protection on $BRANCH"
    gh api "repos/${REPO_SLUG}/branches/${BRANCH}/protection" -q '"required checks=\(.required_status_checks.contexts) enforce_admins=\(.enforce_admins.enabled) force_push=\(.allow_force_pushes.enabled) deletions=\(.allow_deletions.enabled)"' 2>/dev/null \
      || echo "WARNING: $BRANCH is not protected"
    echo "== agent configuration (mode/dry-run/github; token value is never printed)"
    K -n aiops get deploy aiops-agent -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep -E 'AIOPS_(MODE|DRY_RUN|PR_DRY_RUN|GITHUB_ENABLED|GITHUB_REPO|GITHUB_BASE_BRANCH|LLM_PROVIDER)='
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
    out="$(ALLOC=40 git_change_via_pr inject "demo: oom-demo memory demand 12 -> 40 MiB (failure injection)" "ALLOC=40
$EDIT_ALLOC")" || exit 1
    read -r merged_at sha url <<<"$out"
    echo "{\"injected_at\": \"$merged_at\", \"commit\": \"$sha\", \"pr\": \"$url\"}" | tee "$RUNS/last-inject.json"
    echo "Argo CD will sync this commit; watch: $0 status" ;;
  reset)
    # restore the healthy baseline: ALLOC_MB=12 and limits.memory=32Mi (the agent's PR may have raised it)
    out="$(ALLOC=12 git_change_via_pr reset "demo: reset oom-demo to healthy baseline (ALLOC_MB=12, limit 32Mi)" "ALLOC=12
$EDIT_ALLOC
p = pathlib.Path(sys.argv[1]) / 'k8s/gitops/oom-demo/patches/resources.yaml'
p.write_text(re.sub(r'(memory: )\d+Mi', r'\g<1>32Mi', p.read_text()))")" || { rc=$?; [ "$rc" = 3 ] && { echo "already at baseline"; exit 0; }; exit "$rc"; }
    read -r merged_at sha url <<<"$out"
    echo "baseline merged: ${sha:0:7} ($url)" ;;
  measure)
    INC="${2:?usage: $0 measure <incident-id>}"
    exec python3 "$ROOT/aiops/scenarios/tools/gitops_measure.py" "$INC" --context "$CTX" --inject-file "$RUNS/last-inject.json" ;;
  *) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
