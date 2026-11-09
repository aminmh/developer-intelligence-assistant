# Per-host Docker deployment discovery

This optional host stack reports which opted-in project commit runs on one
Docker host. It sends selected label values, the container ID, immutable image
ID, lifecycle state and a provisioned service name. It never sends full Docker
responses, environment variables, logs or unrelated labels.

## Labels

Add these labels to each application service that should be observed:

```yaml
labels:
  com.devassistant.telemetry.enabled: "true"
  com.devassistant.telemetry.project: billing
  com.devassistant.telemetry.environment: staging
  com.devassistant.telemetry.commit: "${GIT_COMMIT_SHA}"
  com.devassistant.telemetry.service: web
```

The agent ignores containers without the exact opt-in label and opted-in
containers with missing or invalid project, environment, commit, service,
container ID or image ID values. Do not put secrets in these labels.

## Host provisioning

Ingress must require a client certificate, validate its chain, strip
`X-Verified-Client-Cert-SHA256` supplied by callers, and set that header from
the verified peer certificate's SHA-256 fingerprint. The backend should be
reachable only through this ingress. Host identity comes from that fingerprint,
never a request body field.

On the backend set `TELEMETRY_HOST_SCOPES` to a JSON object mapping the
lowercase, colon-free certificate fingerprint to allowed project/environment
pairs:

```json
{
  "<64 lowercase hex fingerprint>": {
    "projects": {
      "billing": ["development", "staging"]
    }
  }
}
```

Provision separate certificates and scope entries for development, staging
and production hosts. The same project can be enabled in all three scopes as
needed. Host certificates must be unique per host and rotated through the
existing certificate provisioning process.

Create a host identity directory containing `ca.pem`, `host.crt` and
`host.key`, then configure the Docker socket group ID and backend URL:

```sh
export HOST_IDENTITY_DIR=/etc/devassistant/telemetry-identity
export BACKEND_URL=https://telemetry.example.internal
export DOCKER_SOCK_GID="$(stat -c '%g' /var/run/docker.sock)"
docker compose -f docker-compose.yml up -d --build
```

The proxy (`proxy/`) is a static Go binary that uses only the standard library.
Its image is built from `scratch`: it has no shell, interpreter or package
manager, and the build runs `go vet` and the tests before producing it. It runs
as an unprivileged user in the Docker socket's group, with a read-only
filesystem, no capabilities, `no-new-privileges`, 64 processes and 64 MiB of
memory. The Compose health check runs `/docker-api-proxy -healthcheck`. A `:ro`
mount does not stop API calls over a socket, and socket access is equivalent to
root on the host, so the proxy is the only barrier and is kept deliberately
small:

- It answers only GET `/_ping`, `/version`, `/v1.N/containers/json` and
  `/v1.N/events`. All other methods and paths, including inspect, logs, exec,
  archive, image, network, volume and mutation APIs, are refused.
- It never forwards the client's query string. It rebuilds each Docker request,
  always adds the `com.devassistant.telemetry.enabled=true` label filter, keeps
  only numeric `since` and `until` on events, and asks only for container
  `start`, `die`, `destroy`, `rename`, `pause` and `unpause` events. Health-check
  and exec events, which can carry command lines, never reach the agent.
- It returns only the container ID, image ID, state and `com.devassistant.telemetry.*`
  labels; other containers, commands, mounts, addresses and labels stay in the
  proxy. `/version` returns only the engine and API versions.
- It allows eight concurrent requests, times out Docker calls other than the
  event stream, and logs refusals and failures without request paths.

The proxy has no route to the backend; only the agent has backend egress. The
agent picks a Docker API version from the engine's supported range. It
reconciles at startup, every 60 seconds and about 1.5 seconds after a burst of
lifecycle events, retries failed reports with backoff, and reconnects the event
stream after errors or five idle minutes. The backend retains stopped mappings
for seven days.

Runtime project removal does not add mutation access to this proxy. It can
remove only matching opt-in containers from the local host where the user
started the loopback helper. Remote containers remain running and can report
the project again after its stored Runtime data is purged.

Do not enable production discovery until a real host certificate has completed
an end-to-end request through the production ingress and the backend confirms
the expected fingerprint. The sample compose stack builds locally; promote its
reviewed image digest through the host's normal image promotion process before
deploying beyond staging.

## Local Mac or Linux setup

From a terminal at the Developer Assistant repository root, run:

```sh
just runtime-setup
```

The root `justfile` provides the interactive menu when you run `just` without a
recipe. Install Just and Gum first; on macOS or Linux with Homebrew, run
`brew install just gum`.

The helper needs Python 3, PyYAML, Docker Compose, Git, and OpenSSL. If PyYAML
is missing from the Python 3 environment used by the command, install it there
with `python3 -m pip install PyYAML`. The helper asks before it reads Docker
project metadata, reads the selected project files, changes Compose labels,
updates the local API host scope, starts the ingress/agent, and recreates the
selected services. The Runtime browser guide displays this command; browser
code cannot access your local Docker daemon or files.

Check or remove the local setup with:

```sh
just runtime-status
just runtime-cleanup
```

Cleanup presents one grouped approval for the applicable actions, including
stopping local Runtime services, restoring Compose files, removing host-scope
authorization, refreshing `python-api` when needed, and deleting certificates
generated by setup. Checklist boxes are optional acknowledgements; pressing
Continue approves all listed actions. Cancel preserves the recovery record.
Existing certificates are retained. A private recovery record is stored under
`~/.local/state/devassistant/runtime/` (or `$XDG_STATE_HOME`).

From the repository root, create a development CA and unique server/client certificates:

```sh
./telemetry-dev/host/bootstrap-dev-certs.sh
```

The script refuses to overwrite an existing identity directory. Keep `ca.key`
private and out of source control; the agent and Caddy only need the generated
certificates and private keys mounted read-only. Put the printed SHA-256 host
fingerprint into `TELEMETRY_HOST_SCOPES` in `api/.env`, for example:

```dotenv
TELEMETRY_HOST_SCOPES={"<fingerprint>":{"projects":{"billing":["development"]}}}
```

Start the API ingress first, then the local host agent. The Caddy HTTPS port is
published only on `127.0.0.1`; the local overlay lets the agent reach it over a
private Docker network while preserving certificate verification:

```sh
cd api
docker compose --profile runtime-host up -d python-api telemetry-caddy
BACKEND_URL=https://telemetry-caddy:9443 \
HOST_IDENTITY_DIR="$PWD/telemetry-dev/host/certs/agent" \
docker compose -f telemetry-agent/docker-compose.yml \
  -f telemetry-agent/docker-compose.local.yml up -d --build
```

Docker Desktop for Mac usually exposes `/var/run/docker.sock`. On Linux, set
`DOCKER_SOCK_PATH` if the socket is elsewhere and supply the socket's numeric
group ID so the unprivileged proxy can read it:

```sh
# Linux
export DOCKER_SOCK_GID="$(stat -c '%g' /var/run/docker.sock)"
# macOS / Docker Desktop
export DOCKER_SOCK_GID="$(stat -f '%g' "${DOCKER_SOCK_PATH:-/var/run/docker.sock}")"
```

The agent compose file consumes `HOST_IDENTITY_DIR`, `DOCKER_SOCK_GID`, and
`DOCKER_SOCK_PATH`; `BACKEND_URL` is set to the in-network Caddy address by the
local overlay. The Caddy port can be changed with `TELEMETRY_INGRESS_PORT`.

## Remote deployment

Use `docker-compose.yml` on each remote Docker host and set `BACKEND_URL` to
the HTTPS name exposed by that deployment's trusted ingress. Provision a host
identity directory with `ca.pem`, `host.crt`, and `host.key`; the CA must verify
the ingress server certificate and the client certificate must chain to the CA
configured by the ingress. Do not copy the development CA to production.

On the API deployment, run the bundled Caddy ingress beside `python-api` and
mount the operator-managed server certificate/key and client CA at
`/etc/caddy/identity/{server.crt,server.key,ca.pem}`. The Caddyfile accepts only
`POST /telemetry/host/deployments`, requires a verified client certificate,
replaces the fingerprint header from the TLS peer certificate, and proxies to
the API. Publish Caddy on the intended HTTPS interface and firewall the API so
host reports can reach it only through Caddy. Set `TELEMETRY_INGRESS_BIND` to
the intended interface address (the default is loopback for local use). Configure
`TELEMETRY_HOST_SCOPES` on the API as shown above, with a distinct fingerprint
and least-privilege project/environment pairs for every host.

The same opt-in labels remain the attribution contract for both local and
remote Docker Compose services. Keep the client certificate unique to each
host and rotate its scope entry with the certificate.
