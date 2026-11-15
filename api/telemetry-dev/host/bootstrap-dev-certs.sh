#!/bin/sh
set -eu

out_dir="${1:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/certs}"
umask 077
if [ -e "$out_dir" ]; then
  if [ -L "$out_dir" ] || [ ! -d "$out_dir" ] || find "$out_dir" -mindepth 1 ! -type d -print | grep -q .; then
    echo "Refusing to overwrite existing host identity material in $out_dir" >&2
    exit 1
  fi
fi
mkdir -p "$out_dir/ingress" "$out_dir/agent"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM
cd "$work_dir"

# Key identifiers are required: Python 3.13+ clients (the host agent) verify with
# VERIFY_X509_STRICT and reject certificates without an Authority Key Identifier.
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
  -subj "/CN=DevAssistant Telemetry Development CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" \
  -addext "authorityKeyIdentifier=keyid:always" -out ca.pem

openssl genrsa -out server.key 2048
openssl req -new -key server.key -subj "/CN=caddy" -out server.csr
cat > server.ext <<'EXT'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:telemetry-caddy,DNS:caddy,DNS:localhost,IP:127.0.0.1
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always
EXT
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out server.crt -days 825 -sha256 -extfile server.ext

openssl genrsa -out host.key 2048
openssl req -new -key host.key -subj "/CN=local-docker-host" -out host.csr
cat > host.ext <<'EXT'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always
EXT
openssl x509 -req -in host.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out host.crt -days 825 -sha256 -extfile host.ext

install -m 600 ca.key "$out_dir/ca.key"
install -m 644 ca.pem "$out_dir/ingress/ca.pem"
install -m 644 ca.pem "$out_dir/agent/ca.pem"
install -m 600 server.key "$out_dir/ingress/server.key"
install -m 644 server.crt "$out_dir/ingress/server.crt"
install -m 600 host.key "$out_dir/agent/host.key"
install -m 644 host.crt "$out_dir/agent/host.crt"
fingerprint="$(openssl x509 -in host.crt -noout -fingerprint -sha256 | sed 's/.*=//;s/://g' | tr '[:upper:]' '[:lower:]')"
printf 'Created local development identities in %s\nCA signing key (keep private): %s/ca.key\nHost certificate fingerprint: %s\n' "$out_dir" "$out_dir" "$fingerprint"
printf 'Use this fingerprint as a key in TELEMETRY_HOST_SCOPES.\n'
