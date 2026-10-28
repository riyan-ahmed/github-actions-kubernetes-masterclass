CLUSTER  ?= skillpulse
NAMESPACE ?= skillpulse
BACKEND_IMAGE  ?= trainwithshubham/skillpulse-backend:latest
FRONTEND_IMAGE ?= trainwithshubham/skillpulse-frontend:latest

.PHONY: up down build load apply status logs mysql restart

up: ## One-shot: build images, create cluster, load images, apply manifests
	$(MAKE) build
	kind create cluster --config k8s/kind-config.yaml --name $(CLUSTER)
	$(MAKE) load
	$(MAKE) apply
	@echo
	@echo "  SkillPulse is live at http://localhost:8888"
	@echo

build: ## Build backend + frontend images for the host's architecture
	docker build -t $(BACKEND_IMAGE)  ./backend
	docker build -t $(FRONTEND_IMAGE) ./frontend

load: ## Push built images into the kind node
	kind load docker-image $(BACKEND_IMAGE)  --name $(CLUSTER)
	kind load docker-image $(FRONTEND_IMAGE) --name $(CLUSTER)

apply: ## Apply manifests and wait for rollouts
	kubectl apply -f k8s/00-namespace.yaml \
	              -f k8s/10-mysql.yaml \
	              -f k8s/20-backend.yaml \
	              -f k8s/30-frontend.yaml
	kubectl rollout status statefulset/mysql    -n $(NAMESPACE) --timeout=180s
	kubectl rollout status deployment/backend   -n $(NAMESPACE) --timeout=120s
	kubectl rollout status deployment/frontend  -n $(NAMESPACE) --timeout=60s

down: ## Delete the cluster
	kind delete cluster --name $(CLUSTER)

status: ## Quick health snapshot
	@kubectl get pods,svc,endpoints -n $(NAMESPACE)

logs: ## Tail all three workloads at once
	@kubectl logs -n $(NAMESPACE) -l 'app in (mysql,backend,frontend)' --all-containers --tail=50 -f --max-log-requests=10

mysql: ## Open a mysql shell into the StatefulSet pod
	kubectl exec -it -n $(NAMESPACE) mysql-0 -- mysql -uskillpulse -pskillpulse123 skillpulse

restart: ## Rebuild + reload images, roll backend + frontend
	$(MAKE) build
	$(MAKE) load
	kubectl rollout restart deployment/backend deployment/frontend -n $(NAMESPACE)
	kubectl rollout status  deployment/backend  -n $(NAMESPACE) --timeout=120s
	kubectl rollout status  deployment/frontend -n $(NAMESPACE) --timeout=60s
# Docker cache test

# ---- AIOps (local, offline; none of these touch the cluster) -------------------------------
AIOPS_PY ?= aiops/.venv/bin/python

.PHONY: aiops-venv aiops-test aiops-validate-monitoring aiops-image aiops-image-check aiops-validate-gitops aiops-lint

aiops-venv: ## Create aiops/.venv with the agent + test dependencies
	python3 -m venv aiops/.venv
	aiops/.venv/bin/pip install -q -r aiops/requirements-dev.txt

aiops-test: ## Run the agent unit tests
	$(AIOPS_PY) -m pytest aiops/tests -q -p no:cacheprovider

aiops-validate-monitoring: ## Render Loki/Alloy/Grafana values and test them in local Docker (needs helm + docker)
	$(AIOPS_PY) monitoring/validate/grafana_local_check.py
	$(AIOPS_PY) monitoring/validate/loki_alloy_local_check.py
	$(AIOPS_PY) monitoring/validate/grafana_backup_restore_check.py
	$(AIOPS_PY) monitoring/validate/grafana_to_agent_check.py $(AIOPS_IMAGE)

AIOPS_IMAGE ?= skillpulse-aiops-agent:0.4.0

aiops-image: ## Build the agent image for arm64 (the kind nodes' architecture)
	docker build --platform linux/arm64 -t $(AIOPS_IMAGE) -f aiops/Dockerfile aiops/

aiops-image-check: aiops-image ## Trivy gate (no ignore flags) + container startup/auth/audit checks
	trivy image --quiet --severity HIGH,CRITICAL --exit-code 1 $(AIOPS_IMAGE)
	CHECK_USER=10001:10001 $(AIOPS_PY) aiops/docker/container_check.py $(AIOPS_IMAGE)

aiops-validate-gitops: ## Render the GitOps overlays and validate the Argo CD manifests against the chart's CRD schemas
	@for e in stg prd sandbox; do kubectl kustomize k8s/gitops/$$e >/dev/null && echo "rendered k8s/gitops/$$e"; done
	@for o in approval-dryrun approval-live; do kubectl kustomize k8s/aiops/$$o >/dev/null && echo "rendered k8s/aiops/$$o"; done
	$(AIOPS_PY) gitops/validate_argocd.py

aiops-lint: ## YAML, shell and Python checks on the AIOps files
	yamllint -d "{extends: relaxed, rules: {line-length: {max: 220}}}" aiops/deploy aiops/components aiops/scenarios aiops/policies aiops/kustomization.yaml aiops/docker gitops k8s/gitops k8s/aiops monitoring/aiops-grafana-values.yaml monitoring/alloy-values.yaml monitoring/loki-values.yaml monitoring/grafana-persistence-values.yaml monitoring/grafana-pvc.yaml
	shellcheck -S warning scripts/aiops-decide.sh scripts/seal-db-secret.sh scripts/grafana-alerting-backup.sh scripts/grafana-alerting-restore.sh aiops/scenarios/scenario.sh
	$(AIOPS_PY) -m compileall -q aiops/agent aiops/tests monitoring/validate gitops
