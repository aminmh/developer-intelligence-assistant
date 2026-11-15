---
name: install-developer-assistant
description: Download, install, configure, and verify Developer Intelligence Assistant locally, including optional PHP project telemetry for Laravel, Symfony, or standalone PHP.
---

# Install Developer Intelligence Assistant

Complete a local installation and report verified results. Follow the installation guide in the downloaded checkout as the authority for the version being installed; do not assume development-branch instructions match an older release.

## Prepare

1. Ask for the installation directory if it cannot be inferred, and whether a PHP project should be instrumented now. If so, obtain its path, framework, and whether it runs on the host, Docker Desktop, or the Assistant's Docker network. If no target is available, complete the Assistant installation and provide the telemetry guidance below.
2. Use an existing checkout only after confirming it is the intended project and inspecting its state; do not overwrite or reset it. Otherwise clone `https://github.com/aminmh/developer-intelligence-assistant.git` into the chosen directory. Read its `README.md`, `docs/installation.md`, and installer help before executing it. If the checkout lacks the installer or guide, stop and report the missing files and version.
3. Check the host against the guide: supported macOS or Ubuntu/Debian and architecture, disk and Docker resources, running Docker with Compose/Buildx, Python, Node, Ollama, mkcert, and other listed tools. Start Ollama and confirm the API container can reach it during installer verification. The installer may offer host package changes; do not silently install dependencies, edit hosts, trust certificates, change firewall rules, or expose services without the user's approval of the actual plan.
4. Ask about optional Outline, draw.io, and SonarQube/analyzers, desired hostnames and ports, and local model choices. Keep defaults when the user has no preference. Explain that analyzers require additional memory. Do not request passwords or API keys in chat. Preserve existing `.env` files, credentials, volumes, and user-owned overrides.

## Install and verify

From the checkout, run `bash install.sh check` for preflight, then `bash install.sh` in an interactive terminal. The bootstrap may download a checksum-verified local `just`; the installer collects options and presents the system changes for review. Have the user approve that review; do not feed an automatic approval. If an interactive terminal, privileged action, Docker access, trust-store dialog, or other user action is unavailable, describe the exact action and resume when it is complete. Do not claim installation succeeded on preflight alone.

Use `bash install.sh status`, `bash install.sh resume`, and `bash install.sh info` for status, recovery, and the private service report. Do not print or transmit `.install/services.txt`, `.env` files, tokens, or secrets. Provide the report path and let the user retrieve credentials locally. Never delete volumes, reset accounts, or overwrite conflicting overrides as a recovery shortcut.

Check the installer's final live verification results: HTTPS/trust, OIDC login and logout, Qdrant REST and gRPC authentication, Neo4j Bolt, MinIO, resumable upload, model connectivity from the API container, and Runtime helpers. Include selected optional services' authentication and worker mTLS checks. Confirm the user can open the application and sign in when browser interaction is available. Report headless or browser checks that could not be performed, and distinguish failures from skipped checks. Use the guide's recovery instructions and rerun the verifier after a fix.

## PHP telemetry integration

Read the checkout's Runtime section and the chosen package's installation and configuration guide before changing the target. Ask for a target path if needed, or offer the commands below as guidance. Confirm the target's Composer dependencies, PHP and framework versions, Git state, existing telemetry packages and `.env` handling. Do not install both old `indexr/indexer-otel-*` packages and the new `aminmh/developer-assistant-otel-*` packages: they can expose overlapping PHP namespaces. If an old package is present, stop and plan a migration with the user.

For a compatible target, choose one package in the target project directory:

- Laravel: `composer require aminmh/developer-assistant-otel-laravel`
- Symfony: `composer require aminmh/developer-assistant-otel-symfony`
- Other PHP: `composer require aminmh/developer-assistant-otel-core`; integrate the core using its package guide.

The Laravel and Symfony adapters depend on the core; do not add core separately for those adapters. Install `open-telemetry/exporter-otlp` when required by the package guide. The optional PECL OpenTelemetry extension and matching auto-instrumentation packages enable native hooks; absence limits instrumentation and is not equivalent to a complete trace setup. Check Packagist availability and report an unavailable version; do not claim a Composer install succeeded when indexing is pending. Package guides are in the corresponding public GitHub repositories `aminmh/developer-assistant-otel-laravel`, `aminmh/developer-assistant-otel-symfony`, and `aminmh/developer-assistant-otel-core` under `docs/installation.md`.

Configure the actual project ID and immutable indexed revision, enable telemetry, set the service name, and use the OTLP HTTP endpoint reachable from the target: host `http://127.0.0.1:24318`, Docker Desktop `http://host.docker.internal:24318`, or shared Docker network `http://telemetry-collector-runtime:4318`. Follow the package guide for exporter settings, framework registration, project root, sampling, and optional browser telemetry. Never invent a revision ID or use an example value. Ensure project/revision resource attributes and container labels agree with the indexed source. Start the Runtime services, restart the target when required, generate a request, and verify an attributed trace appears in Runtime for the matching project and revision. If this cannot be checked, report telemetry as unverified.

The checkout's `just php-otel-setup` can install packages, commit Composer files, upload source, change `.env` and Compose configuration, and restart services. Read `scripts/setup-php-telemetry.sh --help` and inspect its package names before use. Some versions still select `indexr/indexer-otel-*` even with `--version`; do not represent those versions as installing the `aminmh` packages. Explain and obtain consent for commits, source upload, and restart; use manual package setup when the installer does not support the selected published package. Never assume `--yes` authorizes these side effects.

## Handoff

Report checkout location, selected components and service URL, checks passed, unresolved failures, and any skipped interactive or telemetry verification. Identify the private credentials report without exposing its contents. If telemetry was deferred, give the applicable package command and guide path.
