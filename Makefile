SHELL := /bin/bash

COMPOSE ?= docker compose
PYTHON_SERVICE ?= python-api
PHP_SERVICE ?= php-ast
NETWORK ?= devassistant_net

.PHONY: help network build up down stop restart logs ps \
        python-shell php-shell python-exec php-exec \
        minio-logs qdrant-logs tusd-logs python-logs php-logs minio-bucket \
        rebuild clean prune

help:
	@echo "Available targets:"
	@echo "  make network         - Create the shared devassistant_net Docker network (idempotent)"
	@echo "  make build           - Build all services"
	@echo "  make up              - Start all services in detached mode"
	@echo "  make down            - Stop and remove all services"
	@echo "  make stop            - Stop all services"
	@echo "  make restart         - Restart all services"
	@echo "  make logs            - Follow logs for all services"
	@echo "  make ps              - Show service status"
	@echo "  make rebuild         - Rebuild and restart all services"
	@echo "  make python-shell    - Open shell in python-api"
	@echo "  make php-shell       - Open shell in php-ast"
	@echo "  make python-exec CMD='...' - Run command in python-api"
	@echo "  make php-exec CMD='...'    - Run command in php-ast"
	@echo "  make python-logs     - Follow python-api logs"
	@echo "  make php-logs        - Follow php-ast logs"
	@echo "  make tusd-logs       - Follow tusd logs"
	@echo "  make minio-logs      - Follow minio logs"
	@echo "  make qdrant-logs     - Follow qdrant logs"
	@echo "  make minio-bucket    - Re-run the tus-uploads bucket creation"
	@echo "  make clean           - Remove compose stack and orphans"
	@echo "  make prune           - Remove unused Docker resources"

network:
	docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK)

build:
	$(COMPOSE) build

up: network
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

stop:
	$(COMPOSE) stop

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

rebuild: network
	$(COMPOSE) down
	$(COMPOSE) build
	$(COMPOSE) up -d

python-shell:
	$(COMPOSE) exec $(PYTHON_SERVICE) /bin/sh

php-shell:
	$(COMPOSE) exec $(PHP_SERVICE) /bin/sh

python-exec:
	@test -n "$(CMD)" || (echo "Usage: make python-exec CMD='your command'"; exit 1)
	$(COMPOSE) exec $(PYTHON_SERVICE) $(CMD)

php-exec:
	@test -n "$(CMD)" || (echo "Usage: make php-exec CMD='your command'"; exit 1)
	$(COMPOSE) exec $(PHP_SERVICE) $(CMD)

python-logs:
	$(COMPOSE) logs -f $(PYTHON_SERVICE)

php-logs:
	$(COMPOSE) logs -f $(PHP_SERVICE)

tusd-logs:
	$(COMPOSE) logs -f tusd

minio-logs:
	$(COMPOSE) logs -f minio

qdrant-logs:
	$(COMPOSE) logs -f qdrant

minio-bucket:
	$(COMPOSE) run --rm minio-init

clean:
	$(COMPOSE) down --remove-orphans

prune:
	docker system prune -af --volumes
