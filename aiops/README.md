# aiops/: the SkillPulse incident-response agent

Python + LangGraph. Read-only by default. Start with [`docs/aiops-architecture.md`](../docs/aiops-architecture.md);
beginners start with [`docs/aiops-learning-guide.md`](../docs/aiops-learning-guide.md).

```
agent/        the service: webhook, LangGraph workflow, collectors, classifier, policy, audit, report
runbooks/     three Markdown runbooks (retrieved with BM25)
policies/     policy.yaml: the deterministic rule book (write allowlist, modes, limits)
deploy/       default Kubernetes manifests (read-only RBAC, Deployment, Service, PVC); applied with `kubectl apply -k aiops/`
components/   opt-in kustomize components (the one narrow write Role for backend-sim)
scenarios/    crash-loop, OOM and readiness failure scenarios + scenario.sh + the observer
docker/       base-image evaluation (Debian slim, distroless, Alpine, Chainguard) and the container check
tests/        359+ tests: unit, integration (fake Kubernetes/Prometheus/Loki), manifests, scripts, safety
audit/        runtime output (git-ignored): audit log, incident reports, checkpoints, Grafana backups
```

```bash
make aiops-venv && make aiops-test     # run the tests
make aiops-lint                        # YAML, shell, Python checks
make aiops-validate-gitops             # Argo CD manifests vs CRD schemas
make aiops-image-check                 # build + Trivy + container checks
```
Opt-in overlays for approval-gated remediation live in `k8s/aiops/` (kustomize cannot nest an overlay inside its own base).
