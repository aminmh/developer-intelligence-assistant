#!/usr/bin/env bash
# Install gVisor (runsc + containerd-shim-runsc-v1) on a PRODUCTION Linux host
# and register it with the host Docker daemon.
#
# Container environments must NOT use this script — they use the nested
# sandbox-runner DinD image instead (docker-compose.sandbox.yml).
set -euo pipefail

if [ -f "/.dockerenv" ] || [ -f "/run/.containerenv" ]; then
  echo "error: refusing to install gVisor inside a container." >&2
  echo "In-container environments use the sandbox-runner DinD image" >&2
  echo "(docker-compose.sandbox.yml) instead of installing on the host." >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64|amd64) arch="x86_64";  platform="${GVISOR_PLATFORM:-kvm}" ;;
  aarch64|arm64) arch="aarch64"; platform="${GVISOR_PLATFORM:-systrap}" ;;
  *) echo "error: unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

rel="${GVISOR_RELEASE:-latest}"
base="https://storage.googleapis.com/gvisor/releases/release"

echo "gVisor release: ${rel} (${arch}), platform: ${platform}"

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

for bin in runsc containerd-shim-runsc-v1; do
  curl -fsSL -o "${tmpdir}/${bin}" "${base}/${rel}/${arch}/${bin}"
  curl -fsSL -o "${tmpdir}/${bin}.sha512" "${base}/${rel}/${arch}/${bin}.sha512"
  expected="$(awk '{print $1}' < "${tmpdir}/${bin}.sha512")"
  actual="$(sha512sum "${tmpdir}/${bin}" | cut -d' ' -f1)"
  if [ "${expected}" != "${actual}" ]; then
    echo "error: ${bin} sha512 mismatch (expected ${expected}, got ${actual})" >&2
    exit 1
  fi
  install -m 0755 "${tmpdir}/${bin}" "/usr/local/bin/${bin}"
  echo "installed /usr/local/bin/${bin}"
done

if [ -f /etc/docker/daemon.json ]; then
  cp -a /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%Y%m%d%H%M%S)
  echo "backed up existing /etc/docker/daemon.json"
fi

{
  echo '{'
  echo '  "runtimes": {'
  echo '    "runsc": {'
  echo '      "path": "/usr/local/bin/runsc",'
  echo "      \"runtimeArgs\": [\"--platform=${platform}\"]"
  echo '    }'
  echo '  }'
  echo '}'
} > /etc/docker/daemon.json

echo
echo "runsc installed and registered with Docker. Restart the daemon to apply:"
echo "  sudo systemctl restart docker   # systemd"
echo "  sudo service docker restart     # sysvinit / upstart"
