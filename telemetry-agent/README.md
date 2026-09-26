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

The proxy is built from the Python 3.13.7 slim base and runs as an unprivileged
user. It mounts the Docker socket read-only and forwards only GET requests to
`/_ping`, `/version`, versioned `/containers/json`, and versioned `/events`.
All other methods and paths, including inspect, logs, exec, archive, image,
network, volume and mutation APIs, are denied. The proxy has no route to the
backend; only the agent has backend egress. Reconciliation runs at startup and
every 60 seconds; Docker events trigger prompt reconciliation. The backend
retains stopped mappings for seven days.

Do not enable production discovery until a real host certificate has completed
an end-to-end request through the production ingress and the backend confirms
the expected fingerprint. The sample compose stack builds locally; promote its
reviewed image digest through the host's normal image promotion process before
deploying beyond staging.

## Local Mac or Linux setup

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
docker compose up -d python-api telemetry-caddy
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
