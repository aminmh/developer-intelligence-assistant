SHELL := /bin/bash

COMPOSE ?= docker compose
SERVICE ?= web
DEV_SERVICE ?= web-dev
NETWORK ?= devassistant_net

.PHONY: help network build up down stop restart logs ps \
        dev dev-build dev-down dev-logs dev-shell \
        shell exec lint rebuild clean prune

help:
	@echo "Available targets:"
	@echo "  make network         - Create the shared devassistant_net Docker network (idempotent)"
	@echo "  make build           - Build the production web image"
	@echo "  make up              - Start the production web service in detached mode"
	@echo "  make down            - Stop and remove the production web service"
	@echo "  make stop            - Stop the production web service"
	@echo "  make restart         - Restart the production web service"
	@echo "  make logs            - Follow production web logs"
	@echo "  make ps              - Show service status"
	@echo "  make shell           - Open shell in the production web container"
	@echo "  make exec CMD='...'  - Run command in the production web container"
	@echo "  make dev-build       - Build the dev web image"
	@echo "  make dev             - Start the dev web service (hot reload) in detached mode"
	@echo "  make dev-down        - Stop and remove the dev web service"
	@echo "  make dev-logs        - Follow dev web logs"
	@echo "  make dev-shell       - Open shell in the dev web container"
	@echo "  make lint            - Run tsc --noEmit inside the dev container"
	@echo "  make rebuild         - Rebuild and restart the production web service"
	@echo "  make clean           - Remove compose stack and orphans"
	@echo "  make prune           - Remove unused Docker resources"

network:
	docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK)

build:
	$(COMPOSE) build $(SERVICE)

up: network
	$(COMPOSE) up -d $(SERVICE)

down:
	$(COMPOSE) down

stop:
	$(COMPOSE) stop $(SERVICE)

restart:
	$(COMPOSE) restart $(SERVICE)

logs:
	$(COMPOSE) logs -f $(SERVICE)

ps:
	$(COMPOSE) ps

shell:
	$(COMPOSE) exec $(SERVICE) /bin/sh

exec:
	@test -n "$(CMD)" || (echo "Usage: make exec CMD='your command'"; exit 1)
	$(COMPOSE) exec $(SERVICE) $(CMD)

dev-build:
	$(COMPOSE) --profile dev build $(DEV_SERVICE)

dev: network
	$(COMPOSE) --profile dev up -d $(DEV_SERVICE)

dev-down:
	$(COMPOSE) --profile dev down

dev-logs:
	$(COMPOSE) logs -f $(DEV_SERVICE)

dev-shell:
	$(COMPOSE) exec $(DEV_SERVICE) /bin/sh

lint:
	$(COMPOSE) exec $(DEV_SERVICE) npm run lint

rebuild: network
	$(COMPOSE) down
	$(COMPOSE) build $(SERVICE)
	$(COMPOSE) up -d $(SERVICE)

clean:
	$(COMPOSE) --profile dev down --remove-orphans

prune:
	docker system prune -af --volumes
