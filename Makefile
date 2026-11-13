# mock-poc runbook. `make help` lists targets.
SHELL := /bin/bash
PY := .venv/bin/python
TEMPORAL_DB := .cache/temporal.db

.PHONY: help doctor setup test images up poc-up kargo kargo-down stop start down tools gitea delivery kagent-apply kagent-key ui demo-ui \
        watch k9s dev dev-stop pipeline pipeline-stop forwards temporal worker leftovers clean

help:
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

doctor: ## check host prerequisites (docker, kubectl, helm, uv, temporal, openshell, node) and .env
	@scripts/doctor.sh

setup: ## python venv with temporalio, httpx, mcp, pytest
	uv venv -q -p 3.12 .venv && uv pip install -q -p .venv -r requirements.txt
$(PY): requirements.txt
	uv venv -q -p 3.12 .venv && uv pip install -q -p .venv -r requirements.txt && touch $(PY)

test: ## unit tests (no cluster): work item workflow, claude auth proxy, demo app
	$(PY) -m pytest -q tests
	cd demo-app && npm test --silent


images: ## build the verify supervisor and kagent harness images
	scripts/images.sh

# ---- the stack: poc cluster (kagent, Substrate, OpenShell) + Gitea + kargo stack (Kargo, Argo CD) --
up: doctor $(PY) ## everything from a clean machine (fast if stopped or running): poc cluster, Gitea, agents, kargo stack, delivery
	scripts/up.sh
	scripts/kargo-up.sh
	scripts/delivery-up.sh
start: up ## same as up: restarts stopped clusters
poc-up: ## only the poc cluster: kagent, OpenShell, Gitea, both agents
	scripts/up.sh
kargo: ## only the kargo stack: kargo-hub (Argo CD, Kargo), kargo-nonprod, kargo-prod, kargo-registry
	scripts/kargo-up.sh
stop: dev-stop ## stop host processes and all clusters without deleting them (keeps installs, images, Gitea PRs, releases)
	-scripts/port-forwards.sh stop
	-docker stop poc-control-plane kargo-hub-control-plane kargo-nonprod-control-plane kargo-prod-control-plane
tools: ## pinned kind + kagent CLIs in .cache/bin
	scripts/tools.sh
gitea: ## local Gitea with the demo-todo repo + webhook to the pipeline (http://localhost:3300)
	scripts/gitea-up.sh
delivery: ## wire into the kargo stack: gitops repo, Kargo project demo-todo, first release, stage edges
	scripts/delivery-up.sh
kagent-apply: ## (re)apply dev-agent + delivery-agent: harness, model, egress, MCP servers, prompts
	scripts/kagent-apply.sh
kagent-key: ## set the Anthropic API key in the cluster from ANTHROPIC_API_KEY (env or .env)
	scripts/kagent-apply.sh key
ui: ## open the kagent UI and the work items page
	open http://localhost:8082 http://localhost:8765
demo-ui: ## open every UI used in docs/DEMO.md: kagent, work items, Temporal, Gitea, Kargo, Argo CD, dev
	open http://localhost:8082 http://localhost:8765 http://localhost:8233 http://localhost:3300/poc/demo-todo \
	  https://localhost:31444/project/demo-todo https://localhost:31443 http://localhost:31081
watch: ## live view: Substrate actors, OpenShell verify sandboxes, Temporal work items, Gitea
	@$(PY) scripts/watch.py
k9s: ## k9s on the poc cluster, starting at the OpenShell verify pods
	k9s --context kind-poc -n openshell

# ---- pipeline: temporal + worker + MCP server -------------------------------
dev: ## temporal + worker + pipeline server + claude auth proxy in the background (APP_RUNTIME=local: verify in docker)
	@scripts/dev.sh start $(or $(APP_RUNTIME),openshell)
dev-stop: ## stop what `make dev` started
	@scripts/dev.sh stop
pipeline: ## pipeline MCP server in the foreground (APP_RUNTIME=openshell|local)
	APP_RUNTIME=$(or $(APP_RUNTIME),openshell) $(PY) worker/pipeline_server.py
pipeline-stop:
	@scripts/dev.sh stop-pipeline

forwards: ## (re)start the kagent UI/API and OpenShell gateway port-forwards
	scripts/port-forwards.sh all

down: stop ## delete the poc cluster; kind-registry, the image cache, Gitea and the kargo stack are kept
	-.cache/bin/kind delete cluster --name poc
kargo-down: ## delete kargo-hub/nonprod/prod; registry kept
	-for c in hub nonprod prod; do .cache/bin/kind delete cluster --name kargo-$$c; done

# ---- foreground processes (for debugging; `make dev` runs them in the background) ----
temporal: ## temporal dev server in the foreground (UI :8233)
	@mkdir -p .cache
	temporal server start-dev --db-filename $(TEMPORAL_DB)
worker: ## the worker in the foreground
	@mkdir -p .cache
	$(PY) worker/main.py 2>&1 | tee -a .cache/worker.log

leftovers: ## anything still running from the POC? (verify sandboxes, preview apps, edges)
	-@command -v openshell >/dev/null && { echo "openshell:"; openshell sandbox list; }
	@echo "preview apps:"; kubectl --context kind-kargo-hub -n argocd get applications -l poc.local/kind=pr-env --no-headers 2>/dev/null || true
	@echo "edges:"; docker ps --filter label=poc.edge=demo-todo --format '  {{.Names}} {{.Status}}'

clean: ## remove the temporal dev db (all workflow history)
	rm -f $(TEMPORAL_DB)
