# syntax=docker/dockerfile:1.7

# phpactor for LSP indexing (lsp/indexer.py), built with Composer in its own stage so the
# worker image itself carries no Composer. The PHP packages match the worker stage below.
FROM python:3.12-slim-trixie AS php-lsp
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates php-cli php-mbstring php-xml composer unzip
COPY Python-Service/php/lsp/composer.json Python-Service/php/lsp/composer.lock /opt/php-lsp/
RUN --mount=type=cache,target=/root/.cache/composer cd /opt/php-lsp \
    && composer install --no-dev --no-interaction --no-progress --no-scripts --optimize-autoloader \
    && php vendor/bin/phpactor --version

FROM python:3.12-slim-trixie

ARG TARGETARCH
ARG JOERN_VERSION=4.0.664
ARG SONAR_SCANNER_VERSION=8.1.0.6389
ARG NODE_VERSION=24.22.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    HOME=/tmp \
    TMPDIR=/tmp \
    SETTINGS_DB_PATH=/tmp/analyzer-settings.db \
    TELEMETRY_SNAPSHOT_ROOT=/tmp/analyzer-snapshots \
    LOG_DIR=/tmp/analyzer-logs

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg openjdk-21-jre-headless unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Keep Sonar's JavaScript analyzer on an immutable executable path. The worker's
# /tmp mount is intentionally noexec, so scanner-provisioned Node cannot run there.
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) node_arch=x64; node_sha=6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6 ;; \
      arm64) node_arch=arm64; node_sha=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e ;; \
      *) echo "Unsupported worker architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl --fail --location --retry 3 --output /tmp/node.tar.xz \
      "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    echo "${node_sha}  /tmp/node.tar.xz" | sha256sum --check; \
    mkdir -p /opt/node; \
    tar -xJf /tmp/node.tar.xz --strip-components=1 -C /opt/node; \
    ln -s /opt/node/bin/node /usr/local/bin/node; \
    node --version; \
    rm /tmp/node.tar.xz

WORKDIR /app
COPY Python-Service/requirements-analyzer-worker.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Verify the official SonarSource detached signature before installing the CLI.
RUN set -eux; \
    mkdir -p /opt/sonar-scanner; \
    cd /tmp; \
    gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys 679F1EE92B19609DE816FDE81DB198F93525EC1A; \
    curl --fail --location --retry 3 --output sonar-scanner.zip \
      "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SONAR_SCANNER_VERSION}.zip"; \
    curl --fail --location --retry 3 --output sonar-scanner.zip.asc \
      "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SONAR_SCANNER_VERSION}.zip.asc"; \
    gpg --batch --verify sonar-scanner.zip.asc sonar-scanner.zip; \
    unzip -q sonar-scanner.zip -d /opt/sonar-scanner; \
    ln -s /opt/sonar-scanner/sonar-scanner-${SONAR_SCANNER_VERSION}/bin/sonar-scanner /usr/local/bin/sonar-scanner; \
    rm -rf /tmp/sonar-scanner.zip /tmp/sonar-scanner.zip.asc /root/.gnupg

# Joern publishes signed release tags and SHA-256 digests for each platform.
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) joern_arch=x86_64; joern_sha=eccb12ca115bf3187918dc9dc43f3277f1cfdb90ff48f5a7ba2af8e7f86f73fc ;; \
      arm64) joern_arch=arm64; joern_sha=cc33983232cca38d3266612f821f87a7fb1aa840d9e275a27b16e5968dc54b92 ;; \
      *) echo "Unsupported worker architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl --fail --location --retry 3 --output /tmp/joern.zip \
      "https://github.com/joernio/joern/releases/download/v${JOERN_VERSION}/joern-cli-linux-${joern_arch}.zip"; \
    echo "${joern_sha}  /tmp/joern.zip" | sha256sum --check; \
    mkdir -p /opt/joern; \
    unzip -q /tmp/joern.zip -d /opt/joern; \
    joern_path="$(find /opt/joern -type f -name joern -perm -u+x -print -quit)"; \
    parser_path="$(find /opt/joern -type f -name joern-parse -perm -u+x -print -quit)"; \
    test -n "${joern_path}" && test -n "${parser_path}"; \
    ln -s "${joern_path}" /usr/local/bin/joern; \
    ln -s "${parser_path}" /usr/local/bin/joern-parse; \
    chmod -R a+rX /opt/joern; \
    rm /tmp/joern.zip

# SCIP indexers for precise references (no project builds or dependency installs).
# scip-typescript and scip-python come from npm at exact versions with install
# scripts disabled; the scip CLI (protobuf -> JSON) is checksum-verified.
ARG SCIP_VERSION=0.10.0
ARG SCIP_TYPESCRIPT_VERSION=0.5.2
ARG SCIP_PYTHON_VERSION=0.6.8
ENV PATH=/opt/node/bin:${PATH}
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) scip_sha=eeb28ebbff443c01609fb1691809958f3f0c7a8651af689a5bc8560061cd8ab0 ;; \
      arm64) scip_sha=6ab677dc2c4bf2955975d0530766152e45daaa988f9404068d8adecacd0bb24c ;; \
      *) echo "Unsupported worker architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl --fail --location --retry 3 --output /tmp/scip.tar.gz \
      "https://github.com/scip-code/scip/releases/download/v${SCIP_VERSION}/scip-linux-${TARGETARCH}.tar.gz"; \
    echo "${scip_sha}  /tmp/scip.tar.gz" | sha256sum --check; \
    mkdir -p /tmp/scip; \
    tar -xzf /tmp/scip.tar.gz -C /tmp/scip; \
    install -m 0755 "$(find /tmp/scip -type f -name scip -print -quit)" /usr/local/bin/scip; \
    rm -rf /tmp/scip /tmp/scip.tar.gz; \
    npm install --global --ignore-scripts --no-audit --no-fund \
      "@sourcegraph/scip-typescript@${SCIP_TYPESCRIPT_VERSION}" "@sourcegraph/scip-python@${SCIP_PYTHON_VERSION}"; \
    npm cache clean --force; \
    scip --version; \
    scip-typescript --version; \
    scip-python --version

# PHP CLI for Joern's PHP frontend (php2cpg runs PHP to parse) and Rector. Distribution
# packages only; no Composer, so no project dependency is ever installed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends php-cli php-mbstring php-xml \
    && rm -rf /var/lib/apt/lists/* \
    && php --version

COPY --from=php-lsp /opt/php-lsp /opt/php-lsp

# App code last, so code changes rebuild only these layers, not the downloads above.
COPY Python-Service/app/code_quality/__init__.py /app/code_quality/__init__.py
COPY Python-Service/app/code_quality/service.py /app/code_quality/service.py
COPY Python-Service/app/code_quality/external_analyzers.py /app/code_quality/external_analyzers.py
COPY Python-Service/app/code_quality/scip_index.py /app/code_quality/scip_index.py
COPY Python-Service/app/infra/settings.py /app/infra/settings.py
COPY Python-Service/app/infra/settings_store.py /app/infra/settings_store.py
COPY Python-Service/app/infra/log_paths.py /app/infra/log_paths.py
COPY Python-Service/app/infra/exception_logger.py /app/infra/exception_logger.py
COPY Python-Service/app/lsp/ /app/lsp/
COPY Python-Service/app/config/lsps.yml /app/config/lsps.yml
COPY analyzer-worker/worker.py /opt/analyzer-worker/worker.py

RUN useradd --system --uid 10001 --create-home --home-dir /tmp/analyzer analyzer \
    && mkdir -p /tmp/analyzer-work \
    && chown -R 10001:10001 /tmp/analyzer-work

USER 10001:10001
EXPOSE 9443
CMD ["python", "/opt/analyzer-worker/worker.py"]
