#!/usr/bin/env bash
set -euo pipefail
umask 077

output_dir=${1:-}
worker_dns_names=${2:-}
worker_ip_addresses=${3:-}

if [[ -z "$output_dir" || -z "$worker_dns_names" ]]; then
  echo "Usage: $0 OUTPUT_DIR WORKER_DNS_NAMES [WORKER_IP_ADDRESSES]" >&2
  echo "Comma-separate DNS names and optional IP addresses; include analyzer-worker for local Production Mode." >&2
  exit 2
fi
if [[ ! "$worker_dns_names" =~ ^[A-Za-z0-9.,-]+$ || ! "$worker_ip_addresses" =~ ^[0-9a-fA-F:.,]*$ ]]; then
  echo "DNS names or IP addresses contain unsupported characters." >&2
  exit 2
fi
command -v openssl >/dev/null 2>&1 || { echo "OpenSSL is required." >&2; exit 1; }

mkdir -p "$output_dir/offline-ca" "$output_dir/api" "$output_dir/worker"
chmod 750 "$output_dir" "$output_dir/api" "$output_dir/worker"
chmod 700 "$output_dir/offline-ca"

worker_ca_key="$output_dir/offline-ca/worker-ca.key"
worker_ca_cert="$output_dir/offline-ca/worker-ca.crt"
api_ca_key="$output_dir/offline-ca/api-ca.key"
api_ca_cert="$output_dir/offline-ca/api-ca.crt"

openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 365 \
  -keyout "$worker_ca_key" -out "$worker_ca_cert" \
  -subj "/CN=Developer Assistant Analyzer Worker CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"
openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 365 \
  -keyout "$api_ca_key" -out "$api_ca_cert" \
  -subj "/CN=Developer Assistant Analyzer API CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

san_list=""
IFS=',' read -r -a dns_names <<< "$worker_dns_names"
for dns_name in "${dns_names[@]}"; do
  [[ -n "$dns_name" ]] || continue
  [[ -z "$san_list" ]] || san_list+=,
  san_list+="DNS:$dns_name"
done
IFS=',' read -r -a ip_addresses <<< "$worker_ip_addresses"
for ip_address in "${ip_addresses[@]}"; do
  [[ -n "$ip_address" ]] || continue
  [[ -z "$san_list" ]] || san_list+=,
  san_list+="IP:$ip_address"
done
[[ -n "$san_list" ]] || { echo "At least one worker DNS name is required." >&2; exit 2; }

worker_ext="$output_dir/worker.ext"
api_ext="$output_dir/api.ext"
printf 'basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' \
  "$san_list" > "$worker_ext"
printf 'basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\n' > "$api_ext"

openssl req -new -newkey rsa:3072 -nodes -sha256 \
  -keyout "$output_dir/worker/worker.key" -out "$output_dir/worker.csr" \
  -subj "/CN=Developer Assistant Analyzer Worker"
openssl x509 -req -sha256 -days 90 -in "$output_dir/worker.csr" \
  -CA "$worker_ca_cert" -CAkey "$worker_ca_key" -CAcreateserial \
  -extfile "$worker_ext" -out "$output_dir/worker/worker.crt"

openssl req -new -newkey rsa:3072 -nodes -sha256 \
  -keyout "$output_dir/api/api.key" -out "$output_dir/api.csr" \
  -subj "/CN=Developer Assistant API Analyzer Client"
openssl x509 -req -sha256 -days 90 -in "$output_dir/api.csr" \
  -CA "$api_ca_cert" -CAkey "$api_ca_key" -CAcreateserial \
  -extfile "$api_ext" -out "$output_dir/api/api.crt"

cp "$worker_ca_cert" "$output_dir/api/worker-ca.crt"
cp "$api_ca_cert" "$output_dir/worker/api-ca.crt"
chmod 640 "$output_dir/worker/worker.key" "$output_dir/api/api.key"
chmod 600 "$worker_ca_key" "$api_ca_key"
chmod 644 "$output_dir/worker/worker.crt" "$output_dir/worker/api-ca.crt" \
  "$output_dir/api/api.crt" "$output_dir/api/worker-ca.crt"
rm -f "$output_dir/worker.csr" "$output_dir/api.csr" "$worker_ext" "$api_ext" \
  "$output_dir/offline-ca/worker-ca.srl" "$output_dir/offline-ca/api-ca.srl"

echo "Created API and worker mTLS bundles under $output_dir."
echo "Keep the API and worker bundles on the Developer Assistant host; only SonarQube receives project source during an opted-in scan."
echo "Set ANALYZER_CERT_GID=$(id -g) so the local non-root worker can read its group-readable key."
echo "Keep offline CA private keys outside both runtime hosts: $output_dir/offline-ca/*.key"
printf 'Worker certificate SHA-256 fingerprint: '
if command -v shasum >/dev/null 2>&1; then
  openssl x509 -in "$output_dir/worker/worker.crt" -outform DER | shasum -a 256 | awk '{print $1}'
else
  openssl x509 -in "$output_dir/worker/worker.crt" -outform DER | sha256sum | awk '{print $1}'
fi
