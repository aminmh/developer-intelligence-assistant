# syntax=docker/dockerfile:1.7

FROM grafana/k6:0.54.0 AS k6

# The PHP packages below are intentionally sourced from the Debian Bookworm
# Sury repository. Pin the Python base so a moving ``slim`` tag cannot switch
# the OS release underneath those packages.
FROM python:3.12-slim-bookworm

COPY --from=k6 /usr/bin/k6 /usr/local/bin/k6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CACHE_DIR=./tmp/pip-cache

# PHP 8.4 CLI (via the Sury PPA on Debian bookworm) + composer for the in-image
# pure-PHP CLI tools (AST chunking + project structure export).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates curl gnupg lsb-release tree openssh-client \
    && curl -sSLo /usr/share/keyrings/sury.gpg https://packages.sury.org/php/apt.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/sury.gpg] https://packages.sury.org/php/ bookworm main" > /etc/apt/sources.list.d/sury.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         php8.4-cli php8.4-mbstring php8.4-zip php8.4-curl php8.4-xml \
         composer git unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./

RUN --mount=type=cache,target=./tmp/pip-cache \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# Bundle both syntax grammars into the image so project parsing never needs a
# first-use grammar download when the API is running offline.
RUN python -c "from tree_sitter_language_pack import download; download(['javascript', 'typescript'])"

# Keep the static analyzer isolated from the API's Python dependencies. Semgrep
# has its own dependency constraints and must not alter the API's JWT stack.
RUN python -m venv /opt/semgrep \
    && /opt/semgrep/bin/pip install --no-cache-dir semgrep==1.181.0 \
    && ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep

# PHP CLI project (nikic/php-parser etc.). Lives outside /app so the dev
# bind-mount of ./Python-Service/app/ -> /app does not shadow it.
COPY php/ /opt/php-cli/
RUN cd /opt/php-cli && composer install --no-dev --no-interaction --no-progress --optimize-autoloader

# PHP language-server toolchain (Phpactor + PHPStan) — validates LLM-generated PHP.
COPY php/lsp/ /opt/php-lsp/
RUN cd /opt/php-lsp && composer install --no-dev --no-interaction --no-progress --optimize-autoloader \
    && mkdir -p /root/.config/phpactor \
    && cat > /root/.config/phpactor/phpactor.json <<'EOF'
{
  "language_server_phpstan.enabled": true,
  "language_server_phpstan.bin": "/opt/php-lsp/vendor/bin/phpstan",
  "language_server_phpstan.config": "/opt/php-lsp/phpstan.neon",
  "language_server.enable_trust_check": false,
  "language_server.diagnostic_sleep_time": 100,
  "language_server.diagnostic_outsource": false,
  "language_server.diagnostics_on_open": true,
  "indexer.enabled_watchers": ["php"]
}
EOF

COPY . .

EXPOSE 9002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9002", "--loop", "asyncio"]
