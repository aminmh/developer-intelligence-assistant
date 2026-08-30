# Task: Nested gVisor Sandbox Runner (DinD) for the Reproduce Pipeline

## Context

This repo runs untrusted user-uploaded ZIP projects. The existing pipeline is:

1. `zip_intake.py` — validates + extracts ZIPs (size caps, zip-slip prevention)
2. `dep_installer.py` — runs `composer install` / `pip install` under **trusted `runc`**
3. `compose_generator.py` — emits `. extracts ZIPs (size caps, zip-slip prevention)
2. `dep_installer.py` — runs `composer install` / `pip install` under **trusted `runc`**
3. `compose_generator.py` — emits `. probe for `runsc` support

Untrusted execution already requests `runtime: runsc` with `read_only: true`,
`cap_drop: ["ALL"]`, `tmpfs` mounts, `pids_limit`, and `mem_limit`.

**Problem:** `runsc` is an OCI runtime binary, not a daemon. It must be registered
with the same Docker daemon that launches the container. On macOS/Apple Silicon dev
machines there is no way to install it on the host. We need a dev-parity path.

## Goal

Add an **optional** nested-daemon backend: a Docker-in-Docker container
(`sandbox-runner`) with `runsc` installed and registered, which the orchestrator
targets via `DOCKER_HOST`. Production on Linux keeps using a host-installed `runsc`.
The pipeline code must be identical in both arrangements.

## Deliverables

### 1. `sandbox-runner/Dockerfile`
- Base `docker:27-dind`
- Download `runsc` + `containerd-shim-runsc-v1` for the build arch from
  `https://storage.googleapis.com/gvisor/releases/release/latest/${arch}`
- **Verify `.sha512` checksums and fail the build if they mismatch.** Do not skip this.
- Install to `/usr/local/bin`, mode `0755`
- Write `/etc/docker/daemon.json` registering the `runsc` runtime with
  `runtimeArgs: ["--platform=systrap"]` (default on ARM64; make it overridable via
  build arg so amd64 hosts can opt into `--platform=kvm`)
- Pin the gVisor release by date rather than `latest` if a pinned URL is reachable;
  otherwise leave `latest` with a `# TODO: pin` comment and note it in the summary

### 2. `docker-compose.dev.yml`
- `sandbox-runner`: `privileged: true`, `DOCKER_TLS_CERTDIR: ""`, listening on
  `tcp://0.0.0.0:2375`
- `orchestrator`: `DOCKER_HOST: tcp://sandbox-runner:2375`, `JOBS_ROOT: /jobs`
- Named volume `jobs` mounted at **`/jobs` in both containers, same path**
- Named volume `dind-storage` at `/var/lib/docker`
- Both on an `internal: true` network. The unauthenticated daemon socket must never
  be reachable from outside the compose network — no `ports:` mapping on 2375.

### 3. Path-resolution fix (the actual bug this exposes)

`compose_generator.build_compose()` embeds `spec.project_root` as a **host** bind
mount source. With a nested daemon that string is resolved by the inner daemon's
filesystem, not the orchestrator's. A path that works with a host runtime silently
mounts an empty directory in the nested case — reproduction then "succeeds" against
nothing, which is worse than failing.

- Introduce `JOBS_ROOT` (env, default `/jobs`) as the single source of truth
- Derive all `project_root` values as `JOBS_ROOT / project_id`
- Add a guard in `ReproduceSpec` (or `build_compose`) that raises if `project_root`
  is not `JOBS_ROOT`-relative after `.resolve()`. Reject symlink escapes too.
- Cover it with a test that asserts the raise

### 4. `sandbox_runtime.py` extension
- Keep the existing `sandbox_available()` contract
- Make it probe whichever daemon `DOCKER_HOST` points at, so the same call answers
  correctly for host-installed and nested backends
- Return enough detail for logs to say *which* backend answered and whether `runsc`
  was found, without changing existing call sites' behaviour

### 5. `scripts/install-gvisor.sh`
- Confirm it still covers the production Linux host path
- Add a guard that refuses to run inside a container, pointing at the DinD path instead

## Constraints

- **`runsc` inside `runsc` is unsupported.** Never apply `runtime: runsc` to
  `sandbox-runner` itself or to the `deps` stage.
- Keep `composer install` / `pip install` on trusted `runc`. Nested overlayfs plus
  systrap compounds the I/O penalty and dependency resolution is the slowest stage.
- The nested backend must stay opt-in. Default behaviour on a Linux host with
  host-installed `runsc` is unchanged, and the pipeline must not branch on backend
  type anywhere except `sandbox_runtime.py`.
- Do not weaken `read_only`, `cap_drop`, `pids_limit`, or `mem_limit` to make
  nesting work. If something genuinely cannot function under those constraints,
  stop and report rather than relaxing them.
- Pin dependency versions exactly. No open ranges.

## Verification

Run before reporting done:
- `mypy` and `ruff` clean on changed Python
- Unit tests for the `JOBS_ROOT` guard, including a symlink-escape case
- `docker compose -f docker-compose.dev.yml config` parses
- `sandbox-runner` image builds and `runsc --version` succeeds inside it
- Integration: `sandbox_available()` returns true against the nested daemon, and a
  trivial job runs end-to-end under `runtime: runsc` with a non-zero-exit case
  correctly surfaced as a failure

If the environment blocks any of these (no nested virt, no network for the gVisor
download), say which check could not run and why. Do not report success on unrun checks.

## Report back

- Files added/changed, one line each
- Which verification steps passed, which were blocked
- **The privilege tradeoff in plain terms:** this adds a gVisor boundary around
  untrusted project code while removing most of the boundary around the runner
  itself. Net gain against a project-code escape, net loss if the runner image or
  the orchestration layer is the weak point. State it explicitly in the summary and
  in a comment at
