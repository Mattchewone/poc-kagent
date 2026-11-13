# mock-poc runbook. `make help` lists targets.
SHELL := /bin/bash
PY := .venv/bin/python
RUNTIME ?= local
SCENARIO ?= happy
IMAGE := mock-poc-agent:dev
TEMPORAL_DB := .cache/temporal.db

.PHONY: help setup test host-check images up stop start down tools gitea kagent-apply kagent-key ui \
        pipeline pipeline-bg pipeline-stop dev dev-stop legacy-up openshell-up openshell-check substrate-up substrate-check \
        substrate-poc forwards temporal worker worker-bg worker-stop run combined approve reject status watch adapters \
        leftovers experiment experiments clean

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

setup: ## python venv with temporalio, httpx, mcp, pytest
	uv venv -q -p 3.12 .venv && uv pip install -q -p .venv -r requirements.txt

test: ## unit tests (no cluster): fake agent, review gate, change workflow, demo app
	$(PY) -m pytest -q tests
	cd demo-app && npm test --silent

host-check: ## milestone 1: tools + kernel >= 6.2 + landlock
	scripts/host-check.sh

images: ## build all images (app supervisor, kagent harness + node, fake agent)
	scripts/images.sh

# ---- the stack: one kind cluster with kagent (+ its Substrate) and OpenShell ---------
up: ## bring everything up (fast if stopped or already running): cluster, kagent, OpenShell, Gitea, dev agent
	scripts/up.sh
start: up ## same as up: restarts a stopped cluster in about a minute
stop: pipeline-stop worker-stop ## stop the cluster without deleting it (keeps installs, images, Gitea PRs)
	-scripts/port-forwards.sh stop
	-docker stop poc-control-plane
tools: ## pinned kind + kagent CLIs in .cache/bin
	scripts/tools.sh
gitea: ## local Gitea with the demo-todo repo (http://localhost:3300)
	scripts/gitea-up.sh
kagent-apply: ## (re)apply the dev agent: harness, model, egress, MCP servers, Agent
	scripts/kagent-apply.sh
kagent-key: ## set the Anthropic API key in the cluster from ANTHROPIC_API_KEY (env or .env)
	scripts/kagent-apply.sh key
ui: ## open the kagent UI and the changes page
	open http://localhost:8082 http://localhost:8765

# ---- pipeline: temporal + worker + MCP server -------------------------------
dev: ## temporal + worker + pipeline MCP server in the background (APP_RUNTIME=local needs no cluster)
	@scripts/dev.sh start $(or $(APP_RUNTIME),openshell)
dev-stop: ## stop what `make dev` started
	@scripts/dev.sh stop
pipeline: ## pipeline MCP server in the foreground (APP_RUNTIME=openshell|local)
	APP_RUNTIME=$(or $(APP_RUNTIME),openshell) $(PY) worker/pipeline_server.py
pipeline-stop:
	@scripts/dev.sh stop-pipeline

# ---- legacy: two clusters, fake agent (milestones 1-8) ----------------------
legacy-up: images openshell-up substrate-up substrate-poc ## old two-cluster setup for the milestone experiments

openshell-up: ## milestone 2: kind + Calico + Agent Sandbox + OpenShell chart + CLI
	scripts/openshell-up.sh
openshell-check: ## milestone 2 exit check
	scripts/openshell-check.sh

substrate-up: ## milestone 3: Substrate kind scripts, counter demo, kubectl-ate
	scripts/substrate-up.sh
substrate-check: ## milestone 3 exit check (counter survives suspend/resume)
	scripts/substrate-check.sh
substrate-poc: images ## push image by digest; atespace poc, WorkerPool, agent + test templates
	scripts/substrate-poc.sh

forwards: ## (re)start the gateway + router port-forwards
	scripts/port-forwards.sh all

down: stop ## delete the cluster(s); registries, pull-through caches and Gitea are kept
	-.cache/bin/kind delete cluster --name poc
	-kind delete cluster --name poc-openshell
	-kind delete cluster --name poc-substrate

# ---- temporal + worker ----------------------------------------------------
temporal: ## temporal dev server with Runtime/Scenario search attributes (UI :8233)
	@mkdir -p .cache
	temporal server start-dev --db-filename $(TEMPORAL_DB) \
	  --search-attribute Runtime=Keyword --search-attribute Scenario=Keyword

worker: ## run the worker in the foreground
	@mkdir -p .cache
	$(PY) worker/main.py 2>&1 | tee -a .cache/worker.log

worker-bg: ## run the worker in the background (needed for the crash experiment)
	@mkdir -p .cache
	@nohup $(PY) worker/main.py >> .cache/worker.log 2>&1 & echo $$! > .cache/worker.pid; \
	  echo "worker pid $$(cat .cache/worker.pid), logs in .cache/worker.log"

worker-stop:
	-@[ -f .cache/worker.pid ] && kill $$(cat .cache/worker.pid) 2>/dev/null; rm -f .cache/worker.pid

# ---- runs -----------------------------------------------------------------
run: ## start a run: make run RUNTIME=openshell SCENARIO=happy [TEST_RUNTIME=openshell] (prints run id)
	$(PY) worker/start.py --runtime $(RUNTIME) --scenario $(SCENARIO) $(if $(TEST_RUNTIME),--test-runtime $(TEST_RUNTIME))
combined: ## agent on Substrate (suspends while waiting), tests in OpenShell sandboxes
	$(PY) worker/start.py --runtime substrate --test-runtime openshell --scenario $(SCENARIO)
approve: ## approve the patch once tests pass: make approve RUN=<id>
	$(PY) worker/start.py approve $(RUN)
reject: ## make reject RUN=<id> [MSG="what to change"] (with MSG the agent revises; without, the run ends)
	$(PY) worker/start.py reject $(RUN) $(if $(MSG),"$(MSG)")
status: ## make status RUN=<id>
	$(PY) worker/start.py status $(RUN)
watch: ## tmux layout, one pane per layer: make watch RUN=<id>
	scripts/watch.sh $(RUN)

adapters: ## milestone 5: same script on each adapter, no Temporal (RUNTIMES="openshell substrate")
	$(PY) worker/adapter_check.py $(or $(RUNTIMES),local)

leftovers: ## anything still running from the POC?
	@echo "local:";     docker ps -a --filter label=poc.run --format '  {{.Names}} {{.Status}}'
	-@command -v openshell >/dev/null && { echo "openshell:"; openshell sandbox list; }
	-@command -v kubectl-ate >/dev/null && { echo "substrate:"; kubectl ate --context kind-poc-substrate get actors -a poc; }

EXPERIMENTS := happy egress cheat never reject revise state crash cancel concurrency long-wait
experiment: ## one row: make experiment EXP=crash RUNTIME=substrate
	scripts/experiment.sh $(EXP) $(RUNTIME)
experiments: ## milestone 8: every row on RUNTIMES (default "openshell substrate")
	@for rt in $(or $(RUNTIMES),openshell substrate); do for e in $(EXPERIMENTS); do scripts/experiment.sh $$e $$rt; done; done

clean: ## remove saved patches and the temporal dev db
	rm -f out/*.patch $(TEMPORAL_DB)
