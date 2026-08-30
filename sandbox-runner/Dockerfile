# syntax=docker/dockerfile:1

# Nested gVisor sandbox runner: a Docker-in-Docker daemon with runsc
# installed and registered, so macOS/Apple Silicon development hosts get a
# runsc-capable daemon without installing gVisor on the host.
#
# Build (from repo root or api/):
#   docker build -f api/sandbox-runner/Dockerfile api/sandbox-runner
#   docker build --build-arg GVISOR_PLATFORM=kvm -f api/sandbox-runner/Dockerfile api/sandbox-runner
#
# NEVER apply runtime:runsc to this container or to the dependency-install
# stage — runsc cannot run inside runsc.

FROM docker:27-dind

ARG TARGETARCH

# systrap is the default platform (works on arm64; kvm is x86_64-only).
ARG GVISOR_PLATFORM=systrap
# Pin a dated gVisor release once one is validated; 'latest' is the fallback.
ARG GVISOR_RELEASE=latest

# TODO: pin — replace 'latest' with a dated release (e.g. 20250520.0) once a
# specific gVisor build is validated against the pipeline. 'latest' stays the
# fallback so the image keeps building if the pinned release disappears from
# object storage.

SHELL ["/bin/sh", "-eu", "-c"]

RUN <<'EOF'
    case "${TARGETARCH:-$(uname -m)}" in
        amd64|x86_64) arch="x86_64" ;;
        arm64|aarch64) arch="aarch64" ;;
        *) echo "unsupported architecture: ${TARGETARCH:-$(uname -m)}" >&2; exit 1 ;;
    esac

    base="https://storage.googleapis.com/gvisor/releases/release"
    rel="${GVISOR_RELEASE}"
    if ! wget -q -O /dev/null "${base}/${rel}/${arch}/runsc.sha512"; then
        echo "gVisor release ${rel} unreachable; falling back to latest" >&2
        rel="latest"
    fi
    url="${base}/${rel}/${arch}"
    echo "installing gVisor release ${rel} (${arch})"

    mkdir -p /opt/gvisor
    for bin in runsc containerd-shim-runsc-v1; do
        wget -q -O "/opt/gvisor/${bin}" "${url}/${bin}"
        wget -q -O "/opt/gvisor/${bin}.sha512" "${url}/${bin}.sha512"
        expected="$(awk '{print $1}' < "/opt/gvisor/${bin}.sha512")"
        actual="$(sha512sum "/opt/gvisor/${bin}" | cut -d' ' -f1)"
        [ "${expected}" = "${actual}" ] || {
            echo "${bin} sha512 mismatch: expected ${expected}, got ${actual}" >&2
            exit 1
        }
        install -m 0755 "/opt/gvisor/${bin}" "/usr/local/bin/${bin}"
    done

    runsc --version
EOF

ARG GVISOR_PLATFORM
RUN mkdir -p /etc/docker && printf '%s\n' \
    '{' \
    '  "runtimes": {' \
    '    "runsc": {' \
    '      "path": "/usr/local/bin/runsc",' \
    '      "runtimeArgs": ["--platform='"${GVISOR_PLATFORM}"'"]' \
    '    }' \
    '  }' \
    '}' > /etc/docker/daemon.json
