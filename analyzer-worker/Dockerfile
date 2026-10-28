# syntax=docker/dockerfile:1.7
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
    TELEMETRY_SNAPSHOT_ROOT=/tmp/analyzer-snapshots

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
COPY Python-Service/app/code_quality/__init__.py /app/code_quality/__init__.py
COPY Python-Service/app/code_quality/service.py /app/code_quality/service.py
COPY Python-Service/app/code_quality/external_analyzers.py /app/code_quality/external_analyzers.py
COPY Python-Service/app/infra/settings.py /app/infra/settings.py
COPY Python-Service/app/infra/settings_store.py /app/infra/settings_store.py
COPY Python-Service/app/infra/log_paths.py /app/infra/log_paths.py
COPY analyzer-worker/worker.py /opt/analyzer-worker/worker.py

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

RUN useradd --system --uid 10001 --create-home --home-dir /tmp/analyzer analyzer \
    && mkdir -p /tmp/analyzer-work \
    && chown -R 10001:10001 /tmp/analyzer-work

USER 10001:10001
EXPOSE 9443
CMD ["python", "/opt/analyzer-worker/worker.py"]
