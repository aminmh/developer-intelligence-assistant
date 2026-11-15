# Local installation and recovery

From the complete downloaded application folder, run:

```bash
bash install.sh
```

The bootstrap checks Just first. It accepts stable Just 1.40+ and otherwise downloads
Just 1.58.0 for macOS Intel/Apple Silicon or Linux x86-64/ARM64 into `.install/bin`.
Each archive has a pinned SHA-256 digest. Verified archives and partial downloads
remain in `.install/downloads`. Nothing is installed into a system binary directory.

The installer reports host dependencies, asks for optional components and editable
addresses, checks published ports, then presents one review. Type `yes` to approve
the displayed dependency, hosts-file and certificate-trust changes. Choosing no
stops before those changes. A first interactive run requires a terminal, including
on headless hosts. Existing installations retain their credentials and data.

## Host requirements

Supported targets are macOS Intel/Apple Silicon and Ubuntu/Debian x86-64/ARM64.
Use a local Docker Linux engine, Docker 24+, Compose 2.24.4+ (required for
`!override` port replacement), Buildx and BuildKit 0.12+, Python 3.10+ with venv,
Node 22+, Ollama 0.11+, mkcert 1.4.4+, curl, tar, OpenSSL, Make, SSH and sudo.
Allow at least 40 GiB free disk, four Docker CPUs and 8 GiB Docker memory;
analyzers require at least 16 GiB Docker memory. Ollama also needs host memory.
The report distinguishes host tools from container-provided application runtimes.

On macOS the approved automatic package step uses existing Homebrew for
mkcert, NSS, Node 22 and Ollama. Install Homebrew and Docker Desktop manually
if missing, start Docker and finish its permission dialogs. Install/select a
supported Python when prompted. Versioned Python executables are detected when
the default macOS Python is too old.

On Ubuntu/Debian the approved package step uses apt for utilities, Python/venv,
mkcert and NSS. Docker Engine/Compose/Buildx, supported Node and Ollama may
require distribution-specific setup. The installer pauses with ACTION items
instead of executing downloaded shell installers or changing Docker groups.
After a Docker group change, log out and back in before resuming.

Start Ollama before the model stage: open Ollama or use
`brew services start ollama` on macOS; use `sudo systemctl start ollama` or
`ollama serve` on Linux. The container API must reach the host gateway on port
11434. Depending on the Ollama installation, set `OLLAMA_HOST=0.0.0.0:11434`
and restart Ollama; restrict host access using your firewall. The final model
probe checks the connection from inside the API container.

Python verification dependencies live in `.install/venv`, never system pip.
Docker images provide the web/API runtimes, PHP/Composer, Go and analyzers.

## Addresses, trust and accounts

Default browser services use HTTPS on loopback port 443:

| Service | Default hostname | Authentication |
| --- | --- | --- |
| Application | app.localhost.dev | Keycloak application login, `developer-admin` |
| Keycloak administration | keycloak.localhost.dev | Separate administration account |
| Qdrant REST | qdrant.localhost.dev | `api-key` header at the HTTPS proxy |
| Neo4j Browser | neo4j.localhost.dev | Database username/password |
| MinIO console | minio.localhost.dev | MinIO root account |
| S3 | s3.localhost.dev | S3 access key and secret, SigV4 |
| Resumable uploads | uploads.localhost.dev | Application-issued upload-session JWT |
| Optional Outline | outline.localhost.dev | Application OIDC account |
| Optional draw.io | drawio.localhost.dev | No standalone account |
| Optional SonarQube/analyzers | sonarqube.localhost.dev | Separate SonarQube account; worker uses mTLS |

Neo4j also publishes encrypted Bolt on loopback port 7687; use the report's
`bolt+s://` address in Neo4j Browser. Qdrant publishes TLS gRPC on loopback
6334 with the same proxy API key. Both ports are editable. Private database
and infrastructure addresses remain private; the report labels them **Internal
only**. Runtime ingress retains its existing independent mTLS policy.

The dedicated Caddy proxy terminates browser HTTPS. mkcert installs its local
root into the host/NSS trust stores; the CA private key stays in mkcert's host
CAROOT and is never mounted into containers. Containers receive only the
certificates they need. The installer reuses valid certificates, checks their
names, issuer, key and expiry, and renews them when necessary. It maintains one
marked `/etc/hosts` block, preserving unrelated entries. Conflicting existing
hostname mappings stop installation. Firefox or another separate browser profile
may need its own `mkcert -install` trust setup.

Application and provider URLs, redirects/origins, secure cookies, uploads, MinIO
console, draw.io and Outline addresses follow the selected configuration. On
resume the installer reconciles persisted OIDC client addresses after checking
their existing secrets. Existing account passwords and client secrets are never
reset to repair a mismatch. For a fresh optional SonarQube database, the installer
replaces the factory administrator password with a generated value. Existing
SonarQube volumes prevent that initialization path.

Retrieve the private report:

```bash
bash install.sh info
# If Just was installed locally, add it to this terminal's PATH:
export PATH="$PWD/.install/bin:$PATH"
just install-info
```

The report is `.install/services.txt`, with file mode `0600` under `.install`
mode `0700`. It contains passwords and API keys: keep it private. Ordinary
progress logs and checkpoints contain redacted output and hashes, not the
credential report. Generated environment files and their original backups are
private too. Installer certificates/configuration stay outside Docker build
contexts; existing Docker ignore rules exclude environment files.

Failed authentication means a stored bootstrap value could differ from a
persisted database account. Update the matching `.env` value using the account's
current credentials, then resume. The installer does not delete volumes or reset
accounts to force authentication to pass. User-owned Compose overrides require
an explicit manual merge; installer-owned overrides carry a generated marker.
Environment files use single-line values; ambiguous dollar interpolation,
multiline values, duplicate keys and symlinked environment files require manual
resolution before configuration generation.

## Progress and recovery

```bash
just install-check    # Dependencies and configured ports; no system changes
just install-status   # Recorded stages and invalidated inputs
just install-resume   # Same recovery entry as bash install.sh
just install          # Review/edit choices before resuming
just install-info    # Show the private report again
```

The Bash progress bar tracks eight stages alongside native download/build
output. Non-terminal output uses ordinary readable lines. Atomic checkpoints
record configuration fingerprints and completed stages. One OS file lock
prevents concurrent installers; it releases on exit, cancellation or reboot.

Each run rechecks dependencies, configuration, certificate trust, startup and
live readiness. Image/build/model checkpoints are reused only after checking
that their artifacts still exist. Changed choices, source, Dockerfiles or
configuration invalidate checkpoints. Failed network/build operations retry
three times with backoff and then print `bash install.sh` as the resume command.

Cancellation and reboot preserve `.install`, downloaded archives, Docker layers,
BuildKit package caches, volumes and Ollama models. The installer never prunes
images, removes volumes or clears caches during recovery. An interrupted
individual operation can restart; completed cached work remains reusable.
Builds retain npm, pip, Composer, apt, Go module and Go compiler caches.
The installed API uses an image-populated volume for its PHP tools so a fresh
folder's development bind mount cannot hide the Composer dependencies.

For terminal automation, create `.install/choices.json` privately before running
and supply the review answer explicitly. Obtain the current schema without
running configuration generation:

```bash
mkdir -p .install && chmod 700 .install
PYTHONPATH=scripts/installer python3 -c \
  'import config,json; print(json.dumps(config.defaults(),indent=2))' > .install/choices.json
chmod 600 .install/choices.json
```

Edit `optional` (`outline`, `drawio`, `sonarqube`), each `hosts` entry,
`bolt_port`, `grpc_port`, and the model list before starting. The first model is
the fresh embedding default; the last is the fresh generation/evaluation default.
Existing model settings take precedence and are also downloaded and revalidated.

## Verification and headless operation

Success requires native HTTPS trust/routes, application OIDC login and logout,
secure session cookies, Qdrant REST authentication and public gRPC, encrypted
authenticated Bolt, MinIO authentication, an authenticated TUS upload with a
resume offset check and finish-hook byte comparison, model embedding from the
API container, and Runtime/system-monitor helpers. Selected analyzers also
require SonarQube authentication and worker mTLS capabilities. The unique tiny
upload probe removes its copied test object; tusd metadata and the test project's
revision metadata may remain. It never deletes existing application uploads.

On a headless machine, native approval dialogs, browser launch and desktop
credential-store integrations are unavailable. Terminal Runtime operations
remain available; browser access requires a browser on that host. LAN access,
public hosting and SSH browser forwarding/trust are separate setup work.

Controlled checks for maintainers:

```bash
bash -n install.sh scripts/installer/bootstrap.sh scripts/installer/install.sh
shellcheck install.sh scripts/installer/bootstrap.sh scripts/installer/install.sh
python3 -m unittest discover -s scripts/installer/tests -v
python3 -m unittest discover -s tests/installer -v
```

These tests use disposable folders/stubs and do not establish fresh-host platform
acceptance. Before release, exercise a fresh and existing installation on all four
OS/architecture combinations, including headless Debian/Ubuntu; interrupt downloads,
pulls/builds and model downloads, reboot and resume; verify browser trust, complete
OIDC login/logout, Neo4j Browser/Bolt, optional Outline login, changed hostnames/ports
and preservation of existing accounts/data. Run the live verifier after each case.
Upgrades and uninstall remain separate work.
