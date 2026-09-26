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
