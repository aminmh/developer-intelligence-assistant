# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CACHE_DIR=./tmp/pip-cache

# PHP 8.4 CLI (via the Sury PPA on Debian bookworm) + composer for the in-image
# pure-PHP CLI tools (AST chunking + project structure export).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates curl gnupg lsb-release tree \
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

# PHP CLI project (nikic/php-parser etc.). Lives outside /app so the dev
# bind-mount of ./Python-Service/app/ -> /app does not shadow it.
COPY php/ /opt/php-cli/
RUN cd /opt/php-cli && composer install --no-dev --no-interaction --no-progress --optimize-autoloader

COPY . .

EXPOSE 9002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9002", "--loop", "asyncio"]
