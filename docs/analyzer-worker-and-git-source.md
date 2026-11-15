# Analyzer worker and Git source

### Analyzer worker and remote SonarQube

The analyzer worker uses the opt-in `analyzer` Compose profile, so ordinary development startup does not
build or start the large analyzer image. Build and run the worker locally from this repository with
`docker compose --profile analyzer up -d analyzer-worker`. The worker image pins SonarScanner and Joern,
runs non-root without Docker socket access, and applies memory, CPU, process, and temporary-storage limits.
The first image build needs several gigabytes of free disk and may take a while.

For a remote worker, generate mutual-TLS certificates on the Developer Assistant host with the worker's
DNS name and IP address:

```sh
api/analyzer-worker/provision-certs.sh /secure/path/analyzer-certs worker.example.local 192.0.2.20
```

Keep `offline-ca/` private keys in secure storage outside both runtime hosts. Keep the `api/` bundle on
the Developer Assistant host. Copy only the `worker/` bundle to the remote host. In External analyzers,
set the worker HTTPS address, fetch its fingerprint from the CA-verified connection, test the connection, and enable
the worker only after consenting to selected-source transfer for that project.

The remote host needs only the pinned worker image, `sonarqube-only.compose.yaml`,
`remote-worker.compose.yaml`, the worker TLS bundle, and a private `.env`; do not copy or check out the
Developer Assistant repository there. Set `SONAR_DB_PASSWORD`, `SONARQUBE_HOST_PORT`, and
`ANALYZER_WORKER_BIND` in `.env`. Run both Compose files as one project:

```sh
docker compose -f sonarqube-only.compose.yaml -f remote-worker.compose.yaml up -d
```

SonarQube binds its management port to loopback (default host port 9000); the worker reaches it over the
private Compose network at `http://sonarqube:9000`. The worker listens only on the configured host
interface and requires mutual TLS. Restrict the worker port to the Developer Assistant host at the host
firewall or private network boundary. Keep SonarQube volumes persistent and backed up. Configure the
project's SonarQube URL as `http://sonarqube:9000`, set its project key, separately consent to SonarQube
source upload, and save the token in the application host OS credential store. The worker sends only the
selected revision to SonarQube during that opted-in scan.

Production Mode keeps the worker and SonarQube on the production application host. Project settings cannot
enable a remote worker or off-host source transfer in Production Mode. Keep the analyzer database and
SonarQube volumes in the environment's backup, retention, upgrade, and rollback procedures; worker job
files remain ephemeral.

The **Git source** tab can connect a project to a GitHub `owner/repository` and branch. Initial
support is HTTPS against GitHub only; private repositories need a fine-grained personal access token
with read-only Contents permission. The desktop helper stores that token in macOS Keychain or Linux
Secret Service, scoped to the project credential ID. It is released for one synchronization request,
used only for `git fetch`, and never persisted by the API. A fetch resolves the branch head to a full
commit SHA and deterministic source archive checksum. Existing matching ready revisions are reused;
new snapshots enter the source-only indexing queue and do not assert that the project is deployed.
Assessments can use the revision after indexing reaches ready. Static assessment supports configured
languages whether or not PHP is present. Semantic search and call tracing remain limited by the PHP
graph exporter; non-PHP revisions show semantic evidence as unavailable. Git submodules are not
supported yet.

Semantic flow tracing currently covers bounded PHP request-input-to-sensitive-operation call paths.
It follows indexed static call edges, cites each symbol, and marks unresolved edges or limits as
partial. A complete trace means complete within those patterns and the selected revision's indexed
PHP business files; it does not prove variable-level data flow or whole-program behavior. Retrieved
semantic excerpts are filtered against the assessment's business-source inventory, excluding
dependency, build, cache, generated, and temporary paths from evaluator context.

To enable optional GitHub push notifications, set `CODE_QUALITY_GITHUB_WEBHOOK_SECRET` in the Web
server environment to the same random secret configured in the GitHub webhook. Use the Webhook URL
`/api/code-quality/github/webhook`, content type `application/json`, and the `push` event. The Web
server verifies GitHub's SHA-256 HMAC signature before resolving the configured repository and
triggering a fresh authenticated fetch. Webhook commits are never accepted directly; manual
**Fetch branch head and reconcile** remains available when notifications are missed or rejected.
